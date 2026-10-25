"""
Booklet_AI Desktop — Secrets Manager

Wraps the keyring library for secure storage of API keys via the
OS credential manager (Windows Credential Manager).

Security rules (STEP-1.1, .continuerules):
- API keys are NEVER stored in plaintext JSON, config files, or logs.
- Keys are retrieved at call time, not cached in memory longer than needed.
- The service name is fixed to prevent key leakage across applications.
"""

import logging
from typing import Optional

import keyring

logger = logging.getLogger(__name__)

# Fixed service name for Windows Credential Manager
_SERVICE_NAME = "BookletAI_Desktop"


class SecretsManager:
    """Secure storage and retrieval of API keys via OS credential manager.

    All keys are stored under the service name 'BookletAI_Desktop'
    in Windows Credential Manager via the keyring library.

    Usage:
        secrets = SecretsManager()
        secrets.set_api_key("openrouter", "sk-...")
        key = secrets.get_api_key("openrouter")
    """

    def __init__(self, service_name: str = _SERVICE_NAME) -> None:
        self._service_name = service_name

    def get_api_key(self, provider: str) -> Optional[str]:
        """Retrieve an API key for the given provider.

        Args:
            provider: Provider identifier (e.g. 'openrouter').

        Returns:
            The API key string, or None if not set.
        """
        try:
            key = keyring.get_password(self._service_name, provider)
            if key:
                # Never log the key itself — AC-1.4.4
                logger.debug("API key retrieved for provider '%s'", provider)
            else:
                logger.debug("No API key found for provider '%s'", provider)
            return key
        except Exception as exc:
            logger.error(
                "Failed to retrieve API key for '%s': %s",
                provider,
                type(exc).__name__,
                # Do NOT log exc details — may contain credential info
            )
            return None

    def set_api_key(self, provider: str, api_key: str) -> None:
        """Store an API key for the given provider.

        Args:
            provider: Provider identifier (e.g. 'openrouter').
            api_key: The API key to store securely.
        """
        try:
            keyring.set_password(self._service_name, provider, api_key)
            logger.info(
                "API key stored for provider '%s' (key NOT logged)",
                provider,
            )
        except Exception as exc:
            logger.error(
                "Failed to store API key for '%s': %s",
                provider,
                type(exc).__name__,
            )
            raise

    def delete_api_key(self, provider: str) -> None:
        """Delete the stored API key for the given provider.

        Args:
            provider: Provider identifier (e.g. 'openrouter').
        """
        try:
            keyring.delete_password(self._service_name, provider)
            logger.info("API key deleted for provider '%s'", provider)
        except keyring.errors.PasswordDeleteError:
            logger.debug(
                "No API key to delete for provider '%s'", provider
            )
        except Exception as exc:
            logger.error(
                "Failed to delete API key for '%s': %s",
                provider,
                type(exc).__name__,
            )
            raise

    def has_api_key(self, provider: str) -> bool:
        """Check whether an API key exists for the given provider.

        Args:
            provider: Provider identifier.

        Returns:
            True if a key is stored, False otherwise.
        """
        return self.get_api_key(provider) is not None
