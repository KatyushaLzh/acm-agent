"""Application service shared by the CLI and the local web interface.

The concrete :class:`AcmService` keeps construction and lifecycle wiring in
one stable entry point.  Domain methods live in focused mixins so the CLI and
web layers keep the same public API without depending on a god class module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .config import Paths
from .credentials import (
    CredentialVault,
    CredentialStoreError,
    DeepSeekCredentialStore,
    create_platform_credential_vault,
)
from .platforms import (
    CodeforcesClient,
    LuoguClient,
    sync_codeforces,
    sync_luogu,
)
from .provider_default import create_default_provider
from .recommend import recommend
from .service_ai import ServiceAIMixin
from .service_common import (
    AIConversationConflict,
    FAILURE_MODES,
    RESULTS,
)
from .service_core import ServiceCoreMixin
from .service_knowledge import ServiceKnowledgeMixin
from .service_plan import ServicePlanMixin
from .service_problem import ServiceProblemMixin
from .storage import Database
from .verify import verify_problem
from .workspace import ProblemRef


class AcmService(
    ServiceCoreMixin,
    ServiceAIMixin,
    ServiceKnowledgeMixin,
    ServiceProblemMixin,
    ServicePlanMixin,
):
    """Structured application API for all stateful ACM workflows.

    Network and compiler dependencies are injectable so HTTP and unit tests do
    not need to monkey-patch implementation modules or launch subprocess CLIs.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        codeforces_client_factory: Callable[[], Any] = CodeforcesClient,
        luogu_client_factory: Callable[[], Any] = LuoguClient,
        sync_codeforces_fn: Callable[..., Any] = sync_codeforces,
        sync_luogu_fn: Callable[..., Any] = sync_luogu,
        verify_fn: Callable[..., Any] = verify_problem,
        provider_client_factory: Callable[[], Any] | None = None,
        deepseek_client_factory: Callable[[], Any] | None = None,
        recommend_fn: Callable[..., Any] | None = None,
        problem_context_fetcher: Callable[[ProblemRef], tuple[str, str]] | None = None,
        credential_store: DeepSeekCredentialStore | None = None,
        credential_vault: CredentialVault | None = None,
    ) -> None:
        self.paths = Paths.for_root(Path(root))
        self._codeforces_client_factory = codeforces_client_factory
        self._luogu_client_factory = luogu_client_factory
        self._sync_codeforces = sync_codeforces_fn
        self._sync_luogu = sync_luogu_fn
        self._verify = verify_fn
        if provider_client_factory is not None and deepseek_client_factory is not None:
            raise ValueError(
                "provider_client_factory and deepseek_client_factory are mutually exclusive"
            )
        selected_provider_factory = provider_client_factory or deepseek_client_factory
        self._provider_factory_is_default = selected_provider_factory is None
        self._provider_client_factory = selected_provider_factory or create_default_provider
        self._recommend_fn = recommend_fn
        self._problem_context_fetcher = problem_context_fetcher
        self._credential_store = credential_store or DeepSeekCredentialStore(
            self.paths.state_dir / "deepseek-key.dpapi"
        )
        self._credential_vault = credential_vault
        if credential_store is None and self._credential_vault is None:
            self._credential_vault = create_platform_credential_vault(self.paths.state_dir)
        self._deepseek_api_key: str | None = None
        self._credential_error: str | None = None
        try:
            if self._credential_vault is not None:
                self._credential_vault.migrate_legacy_deepseek()
            else:
                self._deepseek_api_key = self._credential_store.load()
        except CredentialStoreError as exc:
            self._credential_error = str(exc)
        if self.paths.database.is_file():
            with Database(self.paths.database) as db:
                db.reconcile_interrupted_ai_state()
            self._ensure_builtin_knowledge_targets()
            self._reconcile_ai_patch_proposals()
            self._reconcile_markdown_summary_proposals()

    def _recommend(self, *args: Any, **kwargs: Any) -> Any:
        """Call the injected recommender or the legacy facade patch point."""

        return (self._recommend_fn or recommend)(*args, **kwargs)


__all__ = [
    "AIConversationConflict",
    "AcmService",
    "FAILURE_MODES",
    "RESULTS",
]
