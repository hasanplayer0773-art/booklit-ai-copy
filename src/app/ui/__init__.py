# UI: MainWindow, custom widgets, theme, AI settings, i18n
from app.ui.ai_settings import (
    AISettingsWidget,
    ConnectionTestResult,
    ConnectionTestWorker,
    ModelNameValidationResult,
    test_connection,
    validate_model_name_locally,
)
from app.ui.dashboard_panel import DashboardPanel
from app.ui.error_display import ErrorBanner
from app.ui.error_translation import (
    ERROR_TRANSLATIONS,
    ERROR_TRANSLATIONS_EN,
    EXCEPTION_TRANSLATION_KEYS,
    FALLBACK_MESSAGE,
    FALLBACK_MESSAGE_EN,
    UserFacingError,
    translate_error,
)
from app.ui.i18n import (
    LanguageSelectorWidget,
    TranslationManager,
    get_current_language,
    get_translation_manager,
    set_translation_manager,
    tr,
)
from app.ui.preflight_panel import (
    PreflightPanel,
    PreflightPresentationStatus,
    evaluate_preflight_presentation,
)
from app.ui.problem_report import (
    ProblemReport,
    build_problem_report,
    export_problem_report_json,
)
from app.ui.report_redaction import redact

__all__ = [
    "AISettingsWidget",
    "ConnectionTestResult",
    "ConnectionTestWorker",
    "DashboardPanel",
    "ModelNameValidationResult",
    "test_connection",
    "validate_model_name_locally",
    "ERROR_TRANSLATIONS",
    "ERROR_TRANSLATIONS_EN",
    "EXCEPTION_TRANSLATION_KEYS",
    "FALLBACK_MESSAGE",
    "FALLBACK_MESSAGE_EN",
    "UserFacingError",
    "translate_error",
    "ErrorBanner",
    "LanguageSelectorWidget",
    "TranslationManager",
    "get_translation_manager",
    "set_translation_manager",
    "get_current_language",
    "tr",
    "ProblemReport",
    "build_problem_report",
    "export_problem_report_json",
    "PreflightPanel",
    "PreflightPresentationStatus",
    "evaluate_preflight_presentation",
    "redact",
]
