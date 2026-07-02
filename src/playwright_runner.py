import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

from api_client import ApiClient
from config import settings
from login_executor import LoginExecutor
from workflow_engine import RuntimeContext, WorkflowExecutor


@dataclass
class ExecutionResult:
    ok: bool
    logs: list[str]
    timeline: list[dict]
    video_path: str | None
    screenshot_path: str | None
    execution_id: int | None = None
    artifact_urls: list[str] = field(default_factory=list)


class PlaywrightExecutionEngine:
    def __init__(self, api_client: ApiClient | None = None) -> None:
        self.login_executor = LoginExecutor()
        self.workflow_executor = WorkflowExecutor()
        self.api_client = api_client
        self.default_permissions = [
            "geolocation",
            "notifications",
            "camera",
            "microphone",
            "clipboard-read",
            "clipboard-write",
        ]

    async def run(self, test_name: str, retries: int = 2, login_config: dict | None = None) -> ExecutionResult:
        logs: list[str] = []
        timeline: list[dict] = []
        artifacts_dir = Path("playwright-artifacts")
        artifacts_dir.mkdir(exist_ok=True)
        screenshot_name = self._safe_artifact_name(test_name)

        workflow_runtime_settings = self._extract_workflow_runtime_settings(login_config)

        capture_video = (
            bool(login_config.get("capture_video", False))
            if login_config
            else False
        )
        if not capture_video and workflow_runtime_settings:
            capture_video = bool(workflow_runtime_settings.get("capture_video", False))

        capture_screenshots = (
            bool(login_config.get("capture_screenshots", False))
            if login_config
            else False
        )
        if not capture_screenshots and workflow_runtime_settings:
            capture_screenshots = bool(workflow_runtime_settings.get("capture_screenshots", False))

        navigation_enabled = bool(login_config.get("navigation_enabled", False)) if login_config else False
        navigation_mode = str(login_config.get("navigation_mode", "url")) if login_config else "url"
        navigation_url = str(login_config.get("navigation_url", "")) if login_config else ""
        navigation_section_name = str(login_config.get("navigation_section_name", "")) if login_config else ""
        navigation_wait_ms = int(login_config.get("wait_ms", 3000)) if login_config else 3000
        project_name = str(login_config.get("project_name", "unknown")) if login_config else "unknown"
        login_config_id = login_config.get("id") if login_config else None
        project_id = login_config.get("project_id") if login_config else None
        workflow_definition = self._extract_workflow_definition(login_config)
        has_video_node = self._workflow_has_video_node(workflow_definition)
        if has_video_node:
            capture_video = True

        logs.append(
            "[ARTIFACTS] capture_video="
            f"{capture_video} capture_screenshots={capture_screenshots} artifacts_dir={artifacts_dir}"
        )
        if has_video_node:
            logs.append("[ARTIFACTS] Nodo 'video' detectado en workflow: video habilitado automáticamente")

        # Create execution record in backend
        execution_id: int | None = None
        if self.api_client and project_id:
            try:
                exc_data = await self.api_client.create_execution(project_id, login_config_id)
                if exc_data:
                    execution_id = exc_data.get("id")
                    logs.append(f"Ejecución registrada en backend: ID={execution_id}")
            except Exception as exc:
                logs.append(f"No se pudo registrar ejecución en backend: {exc}")

        iteration = str(int(time.time()))
        artifact_urls: list[str] = []
        uploaded_local_paths: set[str] = set()

        for attempt in range(retries + 1):
            logs.append(f"Intento {attempt + 1} de {retries + 1} para {test_name}")
            timeline.append({"event": "attempt_started", "attempt": attempt + 1})
            if capture_video:
                logs.append(f"[ARTIFACTS] Grabación de video activa desde el inicio del intento {attempt + 1}")

            screenshot_paths: list[str] = []
            video_path_str: str | None = None
            page_video = None
            playwright = None
            browser = None
            context = None
            page = None
            try:
                playwright = await async_playwright().start()
                browser = await playwright.chromium.launch(
                    headless=settings.playwright_headless,
                    slow_mo=settings.playwright_slow_mo_ms,
                )

                context_kwargs: dict = {}
                if capture_video:
                    context_kwargs["record_video_dir"] = str(artifacts_dir)
                    context_kwargs["record_video_size"] = {"width": 1280, "height": 720}

                context = await browser.new_context(**context_kwargs)
                await self._grant_context_permissions(context, login_config, logs)
                page = await context.new_page()
                page_video = page.video if capture_video else None

                if workflow_definition:
                    logs.append("Ejecutando workflow visual incremental")
                    if capture_screenshots:
                        pre_ss = str(artifacts_dir / f"{screenshot_name}_pre_workflow.png")
                        await page.screenshot(path=pre_ss, full_page=True)
                        screenshot_paths.append(pre_ss)
                        logs.append(f"Screenshot pre-workflow: {pre_ss}")

                    runtime_context = RuntimeContext(
                        page=page,
                        logs=logs,
                        timeline=timeline,
                        login_executor=self.login_executor,
                        project_id=int(project_id) if project_id else None,
                        execution_id=int(execution_id) if execution_id else None,
                        api_client=self.api_client,
                        login_config_id=int(login_config_id) if login_config_id else None,
                        artifacts_dir=artifacts_dir,
                        screenshot_name=screenshot_name,
                        capture_screenshots=capture_screenshots,
                        runtime_settings=workflow_runtime_settings,
                        variables={
                            "username": str(login_config.get("username", "")) if login_config else "",
                            "password": str(login_config.get("password", "")) if login_config else "",
                            "url": str(login_config.get("url", "")) if login_config else "",
                            "project_name": project_name,
                            "test_name": test_name,
                        },
                    )
                    workflow_result = await self.workflow_executor.execute(workflow_definition, runtime_context)
                    logs.append(f"Workflow completado. Nodos ejecutados: {len(workflow_result.get('executed_nodes', []))}")

                    for wf_screenshot in runtime_context.evidence_screenshots:
                        if wf_screenshot not in screenshot_paths:
                            screenshot_paths.append(wf_screenshot)

                elif login_config:
                    logs.append("Ejecutando flujo Login con configuración del proyecto")
                    if capture_screenshots:
                        pre_ss = str(artifacts_dir / f"{screenshot_name}_pre_login.png")
                        await page.goto(str(login_config.get("url", "")))
                        await page.screenshot(path=pre_ss, full_page=True)
                        screenshot_paths.append(pre_ss)
                        logs.append(f"Screenshot pre-login: {pre_ss}")

                    login_result = await self.login_executor.execute_login(
                        page=page,
                        url=str(login_config.get("url", "")),
                        username=str(login_config.get("username", "")),
                        password=str(login_config.get("password", "")),
                        has_captcha=bool(login_config.get("has_captcha", False)),
                        playwright_plan=login_config.get("playwright_plan"),
                    )
                    logs.extend(login_result.get("logs", []))
                    if not login_result.get("success", False):
                        raise RuntimeError(login_result.get("message", "Login fallido"))
                else:
                    await page.goto("https://example.com")

                normalized_navigation_mode = navigation_mode.strip().lower()
                has_navigation_data = (
                    bool(navigation_section_name.strip())
                    or bool(navigation_url.strip())
                    or normalized_navigation_mode == "login"
                )

                should_run_navigation = (
                    not workflow_definition
                    and (navigation_enabled or has_navigation_data)
                    and has_navigation_data
                )

                if should_run_navigation:
                    await self._execute_navigation_step(
                        page=page,
                        logs=logs,
                        navigation_mode=navigation_mode,
                        navigation_url=navigation_url,
                        section_name=navigation_section_name,
                        login_config_id=int(login_config_id) if login_config_id else None,
                        navigation_wait_ms=navigation_wait_ms,
                    )

                if capture_screenshots:
                    post_ss = str(artifacts_dir / f"{screenshot_name}_post_login.png")
                    await page.screenshot(path=post_ss, full_page=True)
                    screenshot_paths.append(post_ss)
                    logs.append(f"Screenshot post-login: {post_ss}")

                if context is not None:
                    await context.close()
                    context = None

                # Collect video path from Playwright
                if capture_video and page_video is not None:
                    try:
                        saved = await page_video.path()
                        video_path_str = str(saved) if saved else None
                        if video_path_str:
                            logs.append(f"Video guardado localmente: {video_path_str}")
                    except Exception as video_exc:  # noqa: BLE001
                        logs.append(f"No se pudo recuperar video local: {video_exc}")

                if browser is not None:
                    await browser.close()
                    browser = None
                if playwright is not None:
                    await playwright.stop()
                    playwright = None

                # Upload artifacts to backend (success path)
                if execution_id and self.api_client:
                    await self._upload_artifacts(
                        execution_id=execution_id,
                        screenshot_paths=screenshot_paths,
                        video_path_str=video_path_str,
                        project_name=project_name,
                        test_name=test_name,
                        iteration=iteration,
                        logs=logs,
                        artifact_urls=artifact_urls,
                        uploaded_local_paths=uploaded_local_paths,
                    )

                    await self.api_client.update_execution_status(execution_id, "passed")

                logs.append("Ejecucion completada correctamente")
                timeline.append({"event": "execution_success", "attempt": attempt + 1})
                return ExecutionResult(
                    ok=True,
                    logs=logs,
                    timeline=timeline,
                    video_path=video_path_str if capture_video else None,
                    screenshot_path=screenshot_paths[-1] if screenshot_paths else None,
                    execution_id=execution_id,
                    artifact_urls=artifact_urls,
                )
            except Exception as exc:  # noqa: BLE001
                logs.append(f"Error: {exc}")
                timeline.append({"event": "execution_failed", "attempt": attempt + 1, "error": str(exc)})

                if capture_screenshots and page is not None:
                    try:
                        error_ss = str(artifacts_dir / f"{screenshot_name}_error_attempt_{attempt + 1}.png")
                        await page.screenshot(path=error_ss, full_page=True)
                        screenshot_paths.append(error_ss)
                        logs.append(f"Screenshot de error: {error_ss}")
                    except Exception as screenshot_exc:  # noqa: BLE001
                        logs.append(f"No se pudo capturar screenshot de error: {screenshot_exc}")

                if context is not None:
                    try:
                        await context.close()
                    except Exception as close_exc:  # noqa: BLE001
                        logs.append(f"No se pudo cerrar contexto Playwright: {close_exc}")
                    context = None

                if capture_video and not video_path_str and page_video is not None:
                    try:
                        saved = await page_video.path()
                        video_path_str = str(saved) if saved else None
                        if video_path_str:
                            logs.append(f"Video recuperado tras error: {video_path_str}")
                    except Exception as video_exc:  # noqa: BLE001
                        logs.append(f"No se pudo recuperar video tras error: {video_exc}")

                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    browser = None

                if playwright is not None:
                    try:
                        await playwright.stop()
                    except Exception:
                        pass
                    playwright = None

                # Intenta subir artefactos parciales también en errores.
                if execution_id and self.api_client:
                    try:
                        await self._upload_artifacts(
                            execution_id=execution_id,
                            screenshot_paths=screenshot_paths,
                            video_path_str=video_path_str,
                            project_name=project_name,
                            test_name=test_name,
                            iteration=iteration,
                            logs=logs,
                            artifact_urls=artifact_urls,
                            uploaded_local_paths=uploaded_local_paths,
                        )
                    except Exception as upload_exc:  # noqa: BLE001
                        logs.append(f"No se pudieron subir artefactos parciales: {upload_exc}")

                await asyncio.sleep(0.8)

        if execution_id and self.api_client:
            try:
                await self.api_client.update_execution_status(execution_id, "failed")
            except Exception:
                pass

        return ExecutionResult(
            ok=False,
            logs=logs,
            timeline=timeline,
            video_path=None,
            screenshot_path=None,
            execution_id=execution_id,
            artifact_urls=artifact_urls,
        )

    async def _upload_artifacts(
        self,
        execution_id: int,
        screenshot_paths: list[str],
        video_path_str: str | None,
        project_name: str,
        test_name: str,
        iteration: str,
        logs: list[str],
        artifact_urls: list[str],
        uploaded_local_paths: set[str],
    ) -> None:
        if not self.api_client:
            return

        for ss_path in screenshot_paths:
            if not ss_path or ss_path in uploaded_local_paths:
                continue
            if not Path(ss_path).exists():
                continue

            url = await self.api_client.upload_screenshot(
                execution_id=execution_id,
                file_path=ss_path,
                project_name=project_name,
                test_name=test_name,
                iteration=iteration,
            )
            if url:
                uploaded_local_paths.add(ss_path)
                artifact_urls.append(url)
                logs.append(f"Screenshot subido: {url}")

        if not video_path_str:
            return
        if video_path_str in uploaded_local_paths:
            return
        if not Path(video_path_str).exists():
            return

        url = await self.api_client.upload_video(
            execution_id=execution_id,
            file_path=video_path_str,
            project_name=project_name,
            test_name=test_name,
            iteration=iteration,
        )
        if url:
            uploaded_local_paths.add(video_path_str)
            artifact_urls.append(url)
            logs.append(f"Video subido: {url}")

    def _safe_artifact_name(self, value: str) -> str:
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        normalized = "".join(ch if ch in allowed else "_" for ch in value).strip("_")
        return normalized or "execution"

    async def _grant_context_permissions(
        self,
        context: BrowserContext,
        login_config: dict | None,
        logs: list[str],
    ) -> None:
        try:
            permissions = list(self.default_permissions)

            if isinstance(login_config, dict):
                raw_permissions = login_config.get("permissions")
                if isinstance(raw_permissions, list):
                    for permission in raw_permissions:
                        permission_name = str(permission).strip()
                        if permission_name and permission_name not in permissions:
                            permissions.append(permission_name)

            origins: list[str] = []
            if isinstance(login_config, dict):
                target_url = str(login_config.get("url", "")).strip()
                origin = self._origin_from_url(target_url)
                if origin:
                    origins.append(origin)

                raw_origins = login_config.get("permission_origins")
                if isinstance(raw_origins, list):
                    for raw_origin in raw_origins:
                        candidate = self._origin_from_url(str(raw_origin).strip())
                        if candidate and candidate not in origins:
                            origins.append(candidate)

            if not origins:
                await context.grant_permissions(permissions)
                logs.append(f"Permisos auto-aprobados globalmente: {', '.join(permissions)}")
                return

            for origin in origins:
                await context.grant_permissions(permissions, origin=origin)
                logs.append(f"Permisos auto-aprobados para {origin}: {', '.join(permissions)}")

        except Exception as exc:  # noqa: BLE001
            logs.append(f"No se pudieron auto-aprobar permisos: {exc}")

    def _origin_from_url(self, raw_url: str) -> str | None:
        if not raw_url:
            return None

        parsed = urlparse(raw_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"

        return None

    def _extract_workflow_definition(self, login_config: dict | None) -> dict | None:
        if not isinstance(login_config, dict):
            return None

        workflow_definition = login_config.get("workflow_definition")
        if isinstance(workflow_definition, dict):
            return workflow_definition

        definition = login_config.get("definition")
        if isinstance(definition, dict):
            return definition

        playwright_plan = login_config.get("playwright_plan")
        if isinstance(playwright_plan, dict):
            workflow = playwright_plan.get("workflow")
            if isinstance(workflow, dict):
                return workflow

        raw_nodes = login_config.get("nodes")
        raw_edges = login_config.get("edges")
        if isinstance(raw_nodes, list):
            return {
                "version": str(login_config.get("version", "1.0")),
                "nodes": raw_nodes,
                "edges": raw_edges if isinstance(raw_edges, list) else [],
            }

        return None

    def _extract_workflow_runtime_settings(self, login_config: dict | None) -> dict:
        if not isinstance(login_config, dict):
            return {}

        runtime_settings = login_config.get("runtime_settings")
        if isinstance(runtime_settings, dict):
            return runtime_settings

        workflow_definition = login_config.get("workflow_definition")
        if isinstance(workflow_definition, dict):
            raw_runtime_settings = workflow_definition.get("runtime_settings")
            if isinstance(raw_runtime_settings, dict):
                return raw_runtime_settings

        return {}

    def _workflow_has_video_node(self, workflow_definition: dict | None) -> bool:
        if not isinstance(workflow_definition, dict):
            return False

        nodes = workflow_definition.get("nodes")
        if not isinstance(nodes, list):
            return False

        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_type = str(node.get("type", "")).strip().lower()
            if node_type in {"video", "video_capture", "capture_video"}:
                return True

        return False

    async def _execute_navigation_step(
        self,
        page: Page,
        logs: list[str],
        navigation_mode: str,
        navigation_url: str,
        section_name: str,
        login_config_id: int | None,
        navigation_wait_ms: int,
    ) -> None:
        mode = (navigation_mode or "url").strip().lower()
        if mode not in {"url", "login", "continue"}:
            mode = "url"

        if mode == "url" and navigation_url:
            logs.append(f"[NAV] Navegando a URL objetivo: {navigation_url}")
            await page.goto(navigation_url, wait_until="domcontentloaded")
        elif mode == "continue":
            logs.append("[NAV] Continuando desde el último componente (sin cambiar URL)")
        else:
            logs.append("[NAV] Encadenado desde login: usando página actual")

        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            logs.append("[NAV] Continuando sin networkidle (timeout)")

        if not section_name.strip():
            logs.append("[NAV] Navegación completada sin sección objetivo")
            return

        html_excerpt = (await page.content())[:120000]
        selectors: list[str] = []

        if self.api_client and login_config_id:
            try:
                analysis = await self.api_client.analyze_navigation_html(
                    config_id=login_config_id,
                    section_name=section_name,
                    html_excerpt=html_excerpt,
                    current_url=page.url,
                )
                if isinstance(analysis, dict):
                    raw_selectors = analysis.get("selectors")
                    if isinstance(raw_selectors, list):
                        selectors = [str(item).strip() for item in raw_selectors if str(item).strip()]
                logs.append(f"[NAV] Selectores sugeridos: {len(selectors)}")
            except Exception as exc:  # noqa: BLE001
                logs.append(f"[NAV] No se pudo analizar HTML en backend: {exc}")

        clicked = await self._click_first_available_selector(page, selectors, logs)
        if not clicked:
            clicked = await self._click_by_section_text(page, section_name, logs)

        if not clicked:
            raise RuntimeError(f"No se pudo clickear la sección de navegación '{section_name}'")

        await self._wait_after_navigation_click(
            page=page,
            logs=logs,
            wait_ms=navigation_wait_ms,
        )

        if await self._is_partial_page_load(page):
            logs.append("[NAV] Se detectó carga parcial tras navegar. Aplicando recarga automática...")
            await page.reload(wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                logs.append("[NAV] Recarga ejecutada sin networkidle (timeout)")

            reclicked = await self._click_first_available_selector(page, selectors, logs)
            if not reclicked:
                reclicked = await self._click_by_section_text(page, section_name, logs)

            if reclicked:
                logs.append(f"[NAV] Reintento de click exitoso tras recarga: {section_name}")
                await self._wait_after_navigation_click(
                    page=page,
                    logs=logs,
                    wait_ms=navigation_wait_ms,
                )
            else:
                logs.append("[NAV] No fue necesario o posible reintentar click tras recarga")

        logs.append(f"[NAV] Navegación completada hacia sección: {section_name}")

    async def _is_partial_page_load(self, page: Page) -> bool:
        try:
            is_partial = await page.evaluate(
                """
                () => {
                    const state = document.readyState || '';
                    const body = document.body;
                    if (!body) return true;

                    const main = document.querySelector('main, [role="main"], #app, #root, .app, .main-content');
                    const source = (main && main.innerText) ? main.innerText : body.innerText;
                    const textLength = (source || '').replace(/\\s+/g, ' ').trim().length;

                    return state !== 'complete' || textLength < 40;
                }
                """
            )
            return bool(is_partial)
        except Exception:
            return False

    async def _click_first_available_selector(self, page: Page, selectors: list[str], logs: list[str]) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() <= 0:
                    continue
                await locator.wait_for(state="visible", timeout=5000)
                await locator.click(timeout=5000)
                logs.append(f"[NAV] Click por selector IA: {selector}")
                return True
            except Exception:
                continue
        return False

    async def _click_by_section_text(self, page: Page, section_name: str, logs: list[str]) -> bool:
        if not section_name.strip():
            return False

        normalized = section_name.strip()
        lowered = normalized.lower()
        candidates = [normalized, lowered, normalized.title()]
        fuzzy_pattern = re.compile(r"\\s+".join(re.escape(part) for part in normalized.split()), re.IGNORECASE)

        semantic_locators = [
            page.get_by_role("button", name=fuzzy_pattern),
            page.get_by_role("link", name=fuzzy_pattern),
            page.get_by_role("menuitem", name=fuzzy_pattern),
            page.get_by_role("tab", name=fuzzy_pattern),
            page.get_by_text(fuzzy_pattern),
        ]

        for locator in semantic_locators:
            try:
                if await locator.count() <= 0:
                    continue
                await locator.first.wait_for(state="visible", timeout=5000)
                await locator.first.click(timeout=5000)
                logs.append(f"[NAV] Click fallback semántico: {normalized}")
                return True
            except Exception:
                continue

        for text_value in candidates:
            try:
                btn = page.get_by_role("button", name=text_value)
                if await btn.count() > 0:
                    await btn.first.wait_for(state="visible", timeout=5000)
                    await btn.first.click(timeout=5000)
                    logs.append(f"[NAV] Click fallback por botón: {text_value}")
                    return True
            except Exception:
                pass

            try:
                link = page.get_by_role("link", name=text_value)
                if await link.count() > 0:
                    await link.first.wait_for(state="visible", timeout=5000)
                    await link.first.click(timeout=5000)
                    logs.append(f"[NAV] Click fallback por link: {text_value}")
                    return True
            except Exception:
                pass

            try:
                node = page.get_by_text(text_value).first
                if await node.count() > 0:
                    await node.wait_for(state="visible", timeout=5000)
                    await node.click(timeout=5000)
                    logs.append(f"[NAV] Click fallback por texto: {text_value}")
                    return True
            except Exception:
                pass

        return False

    async def _wait_after_navigation_click(self, page: Page, logs: list[str], wait_ms: int) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            logs.append("[NAV] Click ejecutado, pero sin networkidle (timeout)")

        extra_wait = max(0, int(wait_ms))
        if extra_wait > 0:
            await page.wait_for_timeout(extra_wait)
            logs.append(f"[NAV] Espera adicional tras click: {extra_wait}ms")
