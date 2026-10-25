"""
Booklet_AI Desktop — Build & Release Pipeline (STEP-4.1)

Automates:
1. PyInstaller `--onedir` bundling.
2. Windows Authenticode code signing via `signtool.exe`.
3. Release package archive creation (.zip) and SHA-256 calculation.
4. Static version manifest (`manifest.json`) generation for the update pipeline.
"""
import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

from app.utils.updater import VersionManifest

logger = logging.getLogger("booklet_ai.build_pipeline")


def locate_signtool() -> Optional[Path]:
    """Locate signtool.exe on PATH or in Windows Kits installations."""
    # 1. Check system PATH
    path_hit = shutil.which("signtool.exe") or shutil.which("signtool")
    if path_hit:
        return Path(path_hit)

    # 2. Check standard Windows Kits directories
    sdk_roots = [
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Windows Kits" / "10" / "bin",
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Windows Kits" / "10" / "bin",
    ]
    for sdk_root in sdk_roots:
        if sdk_root.exists():
            matches = list(sdk_root.glob("*/x64/signtool.exe"))
            if matches:
                # Return newest SDK version found
                matches.sort(reverse=True)
                return matches[0]

    return None


def sign_binary(
    binary_path: Path,
    cert_path: Optional[str] = None,
    cert_password: Optional[str] = None,
    thumbprint: Optional[str] = None,
    timestamp_url: str = "http://timestamp.digicert.com",
    signtool_path: Optional[Path] = None,
) -> bool:
    """Sign a Windows executable or DLL with Authenticode via signtool.exe.

    Requires a valid code-signing certificate (AC-4.1.4 precondition).
    Unsigned builds are development-only.

    Args:
        binary_path: Path to the .exe or .dll to sign.
        cert_path: Path to .pfx/.p12 certificate file (optional if thumbprint used).
        cert_password: Password for the certificate file.
        thumbprint: SHA-1 thumbprint of certificate in Windows Certificate Store.
        timestamp_url: RFC 3161 timestamp server URL (SHA-256).
        signtool_path: Explicit path to signtool.exe (optional).

    Returns:
        True if signing succeeded, False otherwise.
    """
    tool = signtool_path or locate_signtool()
    if not tool:
        logger.warning(
            "signtool.exe not found. Binary %s remains unsigned (Development Build Only).",
            binary_path.name,
        )
        return False

    if not cert_path and not thumbprint:
        logger.warning(
            "No code-signing certificate provided. Binary %s remains unsigned (Development Build Only).",
            binary_path.name,
        )
        return False

    cmd: List[str] = [
        str(tool),
        "sign",
        "/fd",
        "sha256",
        "/tr",
        timestamp_url,
        "/td",
        "sha256",
    ]

    if cert_path:
        cmd.extend(["/f", str(cert_path)])
        if cert_password:
            cmd.extend(["/p", cert_password])
    elif thumbprint:
        cmd.extend(["/sha1", thumbprint])

    cmd.append(str(binary_path))

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info("Successfully signed %s: %s", binary_path.name, res.stdout.strip())
        return True
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to sign %s: %s (stderr: %s)", binary_path.name, exc, exc.stderr)
        return False


def build_pyinstaller(
    spec_path: Optional[Path] = None,
    clean: bool = True,
) -> Path:
    """Invoke PyInstaller to generate the --onedir application package.

    Args:
        spec_path: Path to booklet_ai.spec.
        clean: Clean PyInstaller cache before building.

    Returns:
        Path to the output directory containing the application files.
    """
    if spec_path is None:
        spec_candidates = [
            Path.cwd() / "booklet_ai.spec",
            Path(__file__).resolve().parent / "booklet_ai.spec",
            Path(__file__).resolve().parent.parent / "booklet_ai.spec",
        ]
        spec_path = next((p for p in spec_candidates if p.exists()), spec_candidates[0])

    if not spec_path.exists():
        raise FileNotFoundError(f"PyInstaller spec file not found: {spec_path}")

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm"]
    if clean:
        cmd.append("--clean")
    cmd.append(str(spec_path))

    logger.info("Running PyInstaller build with spec: %s", spec_path)
    subprocess.run(cmd, check=True)

    dist_dir = spec_path.parent / "dist" / "Booklet_AI"
    if not dist_dir.exists():
        raise RuntimeError(f"Expected build output directory not found at {dist_dir}")

    logger.info("PyInstaller build complete: %s", dist_dir)
    return dist_dir


def package_distribution(
    dist_dir: Path,
    output_zip_path: Path,
) -> Tuple[Path, str]:
    """Compress the --onedir build folder into a distributable zip archive

    and calculate its SHA-256 hash.

    Args:
        dist_dir: The dist/Booklet_AI directory.
        output_zip_path: Target path for the output .zip file.

    Returns:
        Tuple of (output_zip_path, sha256_hex_digest).
    """
    output_zip_path.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()

    with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for file_path in dist_dir.rglob("*"):
            if file_path.is_file():
                rel_path = file_path.relative_to(dist_dir)
                zipf.write(file_path, arcname=str(rel_path))

    file_bytes = output_zip_path.read_bytes()
    sha256_hash = hashlib.sha256(file_bytes).hexdigest()
    logger.info(
        "Created release package %s (SHA-256: %s)",
        output_zip_path.name,
        sha256_hash,
    )
    return output_zip_path, sha256_hash


def generate_release_manifest(
    version: str,
    download_url: str,
    package_sha256: str,
    release_notes_url: Optional[str] = None,
    output_manifest_path: Optional[Path] = None,
) -> VersionManifest:
    """Generate and write a VersionManifest JSON for update delivery.

    Args:
        version: Version string (e.g. '1.1.0').
        download_url: Download URL for the update package zip.
        package_sha256: Computed SHA-256 hash of the package zip.
        release_notes_url: Optional URL to release notes.
        output_manifest_path: Optional file path to write manifest.json.

    Returns:
        Validated VersionManifest instance.
    """
    manifest = VersionManifest(
        version=version,
        download_url=download_url,
        sha256=package_sha256,
        release_notes_url=release_notes_url,
    )

    if output_manifest_path:
        Path(output_manifest_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_manifest_path).write_text(
            manifest.model_dump_json(indent=2),
            encoding="utf-8",
        )
        logger.info("Wrote release manifest to: %s", output_manifest_path)

    return manifest


def main() -> int:
    """CLI entrypoint for build pipeline."""
    parser = argparse.ArgumentParser(description="Booklet_AI Build & Release Pipeline")
    parser.add_argument("--spec", help="Path to PyInstaller spec file")
    parser.add_argument("--version", default="1.0.0", help="Release version")
    parser.add_argument("--download-url", default="https://releases.example.com/Booklet_AI.zip")
    parser.add_argument("--cert", help="Path to code-signing certificate (.pfx)")
    parser.add_argument("--cert-password", help="Password for certificate")
    parser.add_argument("--thumbprint", help="SHA-1 thumbprint in Windows certificate store")
    parser.add_argument("--no-clean", action="store_true", help="Skip --clean for faster rebuilds")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    spec_path = Path(args.spec) if args.spec else None
    dist_dir = build_pyinstaller(spec_path=spec_path, clean=not args.no_clean)

    # Sign main executable if certificate supplied
    exe_path = dist_dir / "Booklet_AI.exe"
    if exe_path.exists():
        sign_binary(
            binary_path=exe_path,
            cert_path=args.cert,
            cert_password=args.cert_password,
            thumbprint=args.thumbprint,
        )

    # Package into release zip
    zip_path = dist_dir.parent / f"Booklet_AI_{args.version}.zip"
    _, pkg_sha256 = package_distribution(dist_dir, zip_path)

    # Generate release manifest
    manifest_path = dist_dir.parent / "manifest.json"
    generate_release_manifest(
        version=args.version,
        download_url=args.download_url,
        package_sha256=pkg_sha256,
        output_manifest_path=manifest_path,
    )

    logger.info("Build & Release Pipeline finished successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
