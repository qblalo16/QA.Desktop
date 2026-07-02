import base64
import binascii
import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from api_client import ApiClient
from form_fill_ai_service import FormFillAiService
from login_executor import LoginExecutor


FORM_FIELD_SELECTOR_TYPES = {"label", "name", "placeholder", "css", "xpath"}

form_fill_ai_service = FormFillAiService()


@dataclass
class RuntimeContext:
    page: Page
    logs: list[str]
    timeline: list[dict[str, Any]]
    login_executor: LoginExecutor
    project_id: int | None = None
    execution_id: int | None = None
    api_client: ApiClient | None = None
    login_config_id: int | None = None
    artifacts_dir: Path | None = None
    screenshot_name: str = "workflow"
    capture_screenshots: bool = False
    runtime_settings: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    evidence_screenshots: list[str] = field(default_factory=list)
    navigation_text_hits: dict[str, int] = field(default_factory=dict)

    def log(self, message: str) -> None:
        self.logs.append(message)

    def event(self, event: str, **extra: Any) -> None:
        payload = {"event": event, **extra}
        self.timeline.append(payload)


class NodeExecutionError(RuntimeError):
    pass


class NodeExecutor(ABC):
    node_type: str

    @abstractmethod
    async def execute(self, node: dict[str, Any], ctx: RuntimeContext) -> dict[str, Any]:
        raise NotImplementedError


class NodeFactory:
    def __init__(self) -> None:
        self._registry: dict[str, type[NodeExecutor]] = {}

    def register(self, node_type: str, executor_cls: type[NodeExecutor]) -> None:
        self._registry[node_type] = executor_cls

    def create(self, node_type: str) -> NodeExecutor:
        executor_cls = self._registry.get(node_type)
        if not executor_cls:
            raise NodeExecutionError(f"Tipo de nodo no soportado en desktop-runner: {node_type}")
        return executor_cls()

    def available_types(self) -> list[str]:
        return sorted(self._registry.keys())


class WorkflowExecutor:
    def __init__(self, factory: NodeFactory | None = None) -> None:
        self.factory = factory or build_default_node_factory()

    async def execute(self, definition: dict[str, Any], ctx: RuntimeContext) -> dict[str, Any]:
        normalized = self._normalize_definition(definition)
        nodes = normalized.get("nodes", [])
        edges = normalized.get("edges", [])

        node_map = {str(node.get("id", "")).strip(): node for node in nodes if str(node.get("id", "")).strip()}
        order = self._resolve_execution_order(nodes, edges)
        if not order:
            raise NodeExecutionError("Workflow sin nodos ejecutables")
        if len(order) != len(node_map):
            raise NodeExecutionError(
                "Workflow con ciclo o conexiones inválidas. Ajusta conexiones para mantener un flujo lineal/acíclico."
            )

        ctx.event("workflow_started", node_count=len(order))
        executed: list[str] = []

        for node_id in order:
            node = node_map.get(node_id)
            if not node:
                continue
            node_type = str(node.get("type", "")).strip().lower()
            executor = self.factory.create(node_type)
            await self._execute_with_retry(executor, node, ctx)
            executed.append(node_id)

        ctx.event("workflow_finished", executed_nodes=executed)
        return {
            "ok": True,
            "executed_nodes": executed,
            "outputs": ctx.outputs,
        }

    async def _execute_with_retry(self, executor: NodeExecutor, node: dict[str, Any], ctx: RuntimeContext) -> None:
        node_id = str(node.get("id", "unknown"))
        config = node.get("config", {}) if isinstance(node.get("config"), dict) else {}

        runtime_retry = int(ctx.runtime_settings.get("retry_per_node", 0) or 0)
        node_retry = int(config.get("retry", runtime_retry) or 0)
        retry_delay_ms = int(config.get("retry_delay_ms", ctx.runtime_settings.get("retry_delay_ms", 400)) or 400)

        attempts = max(0, node_retry) + 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                ctx.event("node_started", node_id=node_id, node_type=executor.node_type, attempt=attempt)
                result = await executor.execute(node, ctx)
                ctx.outputs[node_id] = result
                ctx.event("node_success", node_id=node_id, node_type=executor.node_type, attempt=attempt)
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                ctx.log(f"[WF] Nodo {node_id} fallo en intento {attempt}/{attempts}: {exc}")
                ctx.event(
                    "node_failed",
                    node_id=node_id,
                    node_type=executor.node_type,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < attempts:
                    await ctx.page.wait_for_timeout(retry_delay_ms)

        raise NodeExecutionError(f"Nodo {node_id} fallo despues de {attempts} intentos: {last_error}")

    def _resolve_execution_order(self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[str]:
        node_ids = [str(node.get("id", "")).strip() for node in nodes if str(node.get("id", "")).strip()]
        indegree: dict[str, int] = {node_id: 0 for node_id in node_ids}
        adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}

        for edge in edges:
            if not isinstance(edge, dict):
                continue
            source = str(edge.get("source", "")).strip()
            target = str(edge.get("target", "")).strip()
            if source in adjacency and target in indegree:
                adjacency[source].append(target)
                indegree[target] += 1

        queue = [node_id for node_id in node_ids if indegree[node_id] == 0]
        ordered: list[str] = []

        while queue:
            current = queue.pop(0)
            ordered.append(current)
            for nxt in adjacency[current]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)

        return ordered

    def _normalize_definition(self, definition: dict[str, Any]) -> dict[str, Any]:
        if isinstance(definition, dict) and isinstance(definition.get("nodes"), list):
            nodes = []
            for index, raw in enumerate(definition.get("nodes", [])):
                if not isinstance(raw, dict):
                    continue
                node_type = self._normalize_node_type(str(raw.get("type", "")).strip().lower())
                nodes.append(
                    {
                        "id": str(raw.get("id", f"node_{index + 1}")) or f"node_{index + 1}",
                        "type": node_type,
                        "label": str(raw.get("label", node_type or "node")),
                        "position": raw.get("position", {}),
                        "config": raw.get("config", {}) if isinstance(raw.get("config"), dict) else {},
                    }
                )
            return {
                "nodes": nodes,
                "edges": definition.get("edges", []) if isinstance(definition.get("edges"), list) else [],
            }

        return {
            "nodes": [],
            "edges": [],
        }

    def _normalize_node_type(self, node_type: str) -> str:
        aliases = {
            "navigation": "navigate",
            "fill_form": "form",
            "upload_file": "upload",
            "video_capture": "video",
            "capture_video": "video",
        }
        return aliases.get(node_type, node_type)


class LoginNodeExecutor(NodeExecutor):
    node_type = "login"

    async def execute(self, node: dict[str, Any], ctx: RuntimeContext) -> dict[str, Any]:
        config = _resolve_config(node.get("config", {}), ctx.variables)
        result = await ctx.login_executor.execute_login(
            page=ctx.page,
            url=str(config.get("url", "")),
            username=str(config.get("username", "")),
            password=str(config.get("password", "")),
            has_captcha=bool(config.get("has_captcha", False)),
            playwright_plan=config.get("playwright_plan") if isinstance(config.get("playwright_plan"), dict) else None,
        )
        ctx.logs.extend(result.get("logs", []))
        if not result.get("success", False):
            raise NodeExecutionError(result.get("message", "Login fallido"))

        if ctx.capture_screenshots and ctx.artifacts_dir:
            screenshot_path = str(ctx.artifacts_dir / f"{ctx.screenshot_name}_after_login.png")
            await ctx.page.screenshot(path=screenshot_path, full_page=True)
            ctx.evidence_screenshots.append(screenshot_path)
            ctx.log(f"[WF] Screenshot login: {screenshot_path}")

        return {"message": result.get("message", "Login ejecutado")}


class NavigateNodeExecutor(NodeExecutor):
    node_type = "navigate"

    async def execute(self, node: dict[str, Any], ctx: RuntimeContext) -> dict[str, Any]:
        config = _resolve_config(node.get("config", {}), ctx.variables)
        mode = str(config.get("navigation_mode", "url")).strip().lower()
        target_url = str(config.get("navigation_url", "")).strip()
        section_name = str(config.get("navigation_section_name", "")).strip()
        wait_ms = int(config.get("wait_ms", ctx.runtime_settings.get("wait_after_navigation_ms", 3000)) or 0)

        if mode == "url" and target_url:
            ctx.log(f"[WF] Navegando a URL: {target_url}")
            await ctx.page.goto(target_url, wait_until="domcontentloaded")
        else:
            ctx.log("[WF] Navegacion encadenada desde pagina actual")

        try:
            await ctx.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            ctx.log("[WF] Navegacion sin networkidle (timeout)")

        used_selectors: list[str] = []
        if section_name:
            occurrence = self._consume_navigation_occurrence(ctx, section_name)
            ctx.log(f"[WF] Navegando por texto '{section_name}' (ocurrencia {occurrence})")

            await self._wait_for_navigation_target(
                ctx.page,
                section_name,
                ctx.log,
                wait_ms=min(wait_ms, 8000),
                occurrence=occurrence,
            )
            used_selectors = await self._get_navigation_selectors(ctx, section_name)
            clicked = await self._click_by_selectors(ctx.page, used_selectors, occurrence=occurrence)
            if not clicked:
                clicked = await self._click_by_text(ctx.page, section_name, occurrence=occurrence)
            if not clicked:
                await self._wait_for_navigation_target(
                    ctx.page,
                    section_name,
                    ctx.log,
                    wait_ms=5000,
                    occurrence=occurrence,
                )
                clicked = await self._click_by_selectors(ctx.page, used_selectors, occurrence=occurrence)
                if not clicked:
                    clicked = await self._click_by_text(ctx.page, section_name, occurrence=occurrence)
            if not clicked:
                raise NodeExecutionError(
                    f"No se pudo navegar a la seccion '{section_name}' (ocurrencia {occurrence})"
                )

            await self._wait_after_navigation_click(ctx, wait_ms)

        if await self._is_partial_page_load(ctx.page):
            ctx.log("[WF] Carga parcial detectada. Recargando y reintentando navegación...")
            await ctx.page.reload(wait_until="domcontentloaded")
            try:
                await ctx.page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                ctx.log("[WF] Recarga sin networkidle (timeout)")

            if section_name:
                reclicked = await self._click_by_selectors(ctx.page, used_selectors, occurrence=occurrence)
                if not reclicked:
                    reclicked = await self._click_by_text(ctx.page, section_name, occurrence=occurrence)
                if reclicked:
                    ctx.log(f"[WF] Reintento de click exitoso tras recarga: {section_name}")
                    await self._wait_after_navigation_click(ctx, wait_ms)

        return {
            "url": ctx.page.url,
            "section": section_name,
            "selectors": used_selectors,
        }

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

    async def _get_navigation_selectors(self, ctx: RuntimeContext, section_name: str) -> list[str]:
        selectors: list[str] = []
        if not ctx.api_client or not ctx.login_config_id:
            return selectors

        try:
            html_excerpt = (await ctx.page.content())[:120000]
            analysis = await ctx.api_client.analyze_navigation_html(
                config_id=ctx.login_config_id,
                section_name=section_name,
                html_excerpt=html_excerpt,
                current_url=ctx.page.url,
            )
            if isinstance(analysis, dict):
                raw = analysis.get("selectors")
                if isinstance(raw, list):
                    selectors = [str(item).strip() for item in raw if str(item).strip()]
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"[WF] Analisis IA de navegacion no disponible: {exc}")

        return selectors

    def _consume_navigation_occurrence(self, ctx: RuntimeContext, section_name: str) -> int:
        key = self._normalize_navigation_key(section_name)
        current = int(ctx.navigation_text_hits.get(key, 0) or 0)
        next_occurrence = current + 1
        ctx.navigation_text_hits[key] = next_occurrence
        return next_occurrence

    def _normalize_navigation_key(self, section_name: str) -> str:
        compact = re.sub(r"\s+", " ", section_name or "").strip().lower()
        return compact

    async def _click_by_selectors(self, page: Page, selectors: list[str], occurrence: int = 1) -> bool:
        target_index = max(0, occurrence - 1)
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = await locator.count()
                if count <= 0:
                    continue
                if target_index >= count:
                    continue

                target = locator.nth(target_index)
                await target.wait_for(state="visible", timeout=5000)
                await target.click(timeout=5000)
                return True
            except Exception:
                continue
        return False

    async def _click_by_text(self, page: Page, section_name: str, occurrence: int = 1) -> bool:
        target_index = max(0, occurrence - 1)
        normalized = section_name.strip()
        candidates = [normalized, normalized.lower(), normalized.title()]
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
                count = await locator.count()
                if count <= 0:
                    continue
                if target_index >= count:
                    continue

                target = locator.nth(target_index)
                await target.wait_for(state="visible", timeout=5000)
                await target.click(timeout=5000)
                return True
            except Exception:
                continue

        for text in candidates:
            try:
                button = page.get_by_role("button", name=text)
                count = await button.count()
                if count > 0:
                    if target_index >= count:
                        continue
                    target = button.nth(target_index)
                    await target.wait_for(state="visible", timeout=5000)
                    await target.click(timeout=5000)
                    return True
            except Exception:
                pass

            try:
                link = page.get_by_role("link", name=text)
                count = await link.count()
                if count > 0:
                    if target_index >= count:
                        continue
                    target = link.nth(target_index)
                    await target.wait_for(state="visible", timeout=5000)
                    await target.click(timeout=5000)
                    return True
            except Exception:
                pass

            try:
                target = page.get_by_text(text)
                count = await target.count()
                if count > 0:
                    if target_index >= count:
                        continue
                    candidate = target.nth(target_index)
                    await candidate.wait_for(state="visible", timeout=5000)
                    await candidate.click(timeout=5000)
                    return True
            except Exception:
                pass

        return False

    async def _wait_for_navigation_target(
        self,
        page: Page,
        section_name: str,
        log_fn,
        wait_ms: int,
        occurrence: int = 1,
    ) -> None:
        if not section_name.strip() or wait_ms <= 0:
            return

        deadline = time.monotonic() + wait_ms / 1000
        target_index = max(0, occurrence - 1)
        locators = [
            page.get_by_role("button", name=re.compile(re.escape(section_name), re.IGNORECASE)),
            page.get_by_role("link", name=re.compile(re.escape(section_name), re.IGNORECASE)),
            page.get_by_role("menuitem", name=re.compile(re.escape(section_name), re.IGNORECASE)),
            page.get_by_role("tab", name=re.compile(re.escape(section_name), re.IGNORECASE)),
            page.get_by_text(re.compile(re.escape(section_name), re.IGNORECASE)),
        ]

        while time.monotonic() < deadline:
            for locator in locators:
                try:
                    count = await locator.count()
                    if count <= 0:
                        continue
                    if target_index >= count:
                        continue

                    candidate = locator.nth(target_index)
                    if await candidate.is_visible():
                        log_fn(
                            f"[WF] Se detectó visible la sección objetivo: {section_name} (ocurrencia {occurrence})"
                        )
                        return
                except Exception:
                    continue
            await page.wait_for_timeout(500)

        log_fn(f"[WF] La sección '{section_name}' aún no estaba visible tras esperar {wait_ms}ms")

    async def _wait_after_navigation_click(self, ctx: RuntimeContext, wait_ms: int) -> None:
        try:
            await ctx.page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            ctx.log("[WF] Click ejecutado, pero sin networkidle (timeout)")

        extra_wait = max(0, int(wait_ms))
        if extra_wait > 0:
            await ctx.page.wait_for_timeout(extra_wait)
            ctx.log(f"[WF] Espera adicional tras click: {extra_wait}ms")


class FillFormNodeExecutor(NodeExecutor):
    node_type = "form"

    _SAMPLE_VALUES = {
        "date": "15/01/2024",
        "email": "qa.automatizacion@example.com",
        "phone": "5512345678",
        "currency": "150000",
        "number": "12345",
        "rfc": "XAXX010101000",
        "name": "Prueba Automatizada",
        "address": "Calle Falsa 123",
        "text": "Dato de prueba",
    }

    def __init__(self) -> None:
        self.ai_service = form_fill_ai_service

    async def execute(self, node: dict[str, Any], ctx: RuntimeContext) -> dict[str, Any]:
        config = _resolve_config(node.get("config", {}), ctx.variables)
        fill_mode = str(config.get("fill_mode", "manual")).strip().lower()
        submit_action: dict[str, Any] = {"intent": "none", "text": ""}

        if fill_mode == "ai":
            form_html_context = await self._capture_base_form_html_context(ctx.page)
            catalog_entries = await self._capture_catalog_entries(ctx.page, ctx.log)
            node_label = str(node.get("label", "formulario")).strip() or "formulario"
            ctx.log(
                f"[WF] Contexto IA: form_html={len(form_html_context)} chars, "
                f"catalogos={len(catalog_entries)}"
            )
            if ctx.api_client and form_html_context.strip():
                try:
                    await ctx.api_client.create_desktop_page_html(
                        html_content=form_html_context,
                        page_url=ctx.page.url,
                        page_title=await ctx.page.title(),
                        source="desktop-runner/form-html-context",
                        project_id=ctx.project_id,
                        execution_id=ctx.execution_id,
                        login_config_id=ctx.login_config_id,
                        workflow_node_id=str(node.get("id", "")),
                    )
                    ctx.log("[WF] HTML guardado en backend (desktop-traces/page-html)")
                except Exception as trace_exc:  # noqa: BLE001
                    ctx.log(f"[WF] No se pudo persistir HTML en backend: {trace_exc}")

            ai_payload = await self.ai_service.generate_fields_payload(
                html_context=form_html_context,
                catalog_entries=catalog_entries,
                page_url=ctx.page.url,
                page_title=await ctx.page.title(),
                node_label=node_label,
            )
            fields = ai_payload.get("fields") if isinstance(ai_payload, dict) else []
            submit_action = ai_payload.get("submit_action") if isinstance(ai_payload, dict) else submit_action
            if not fields:
                raise NodeExecutionError("Azure OpenAI no devolvió campos para el llenado automático")

            if ctx.api_client:
                try:
                    await ctx.api_client.create_desktop_ai_json(
                        response_json={
                            "fields": fields,
                            "submit_action": submit_action,
                            "catalog_context": str(ai_payload.get("catalog_context", "")),
                        },
                        raw_response_text=str(ai_payload.get("raw_response", ""))[:60000],
                        prompt_text=str(ai_payload.get("prompt", ""))[:60000],
                        model_name=str(ai_payload.get("model_name", "")),
                        page_url=ctx.page.url,
                        page_title=await ctx.page.title(),
                        source="desktop-runner/form-fill-ai",
                        project_id=ctx.project_id,
                        execution_id=ctx.execution_id,
                        login_config_id=ctx.login_config_id,
                        workflow_node_id=str(node.get("id", "")),
                    )
                    ctx.log("[WF] JSON IA guardado en backend (desktop-traces/ai-json)")
                except Exception as trace_exc:  # noqa: BLE001
                    ctx.log(f"[WF] No se pudo persistir JSON IA en backend: {trace_exc}")

            ctx.log(f"[WF] IA generó {len(fields)} campo(s) para el formulario")
        else:
            fields = _extract_form_fields(config)

        if not fields:
            raise NodeExecutionError("Nodo form requiere campos para completar")

        field_name_index = await self._build_form_field_name_index(ctx.page, ctx.log)

        completed = 0
        for field in fields:
            value = self._resolve_field_value(field)
            try:
                candidates = self._build_field_locators(
                    ctx.page,
                    field,
                    field_name_index=field_name_index,
                )
                if not candidates:
                    continue

                filled = False
                last_candidate_error: Exception | None = None
                for locator in candidates:
                    try:
                        
                        if await locator.count() <= 0:
                            continue
                        target = locator.first
                        await target.wait_for(state="visible", timeout=2500)
                        await self._fill_or_select_field(ctx.page, target, field, value, ctx.log)
                        filled = True
                        completed += 1
                        break
                    except Exception as candidate_exc:  # noqa: BLE001
                        last_candidate_error = candidate_exc
                        continue

                if not filled:
                    raise NodeExecutionError(
                        f"No se encontró un locator visible para el campo usando múltiples estrategias: {last_candidate_error}"
                    )
            except Exception as exc:  # noqa: BLE001
                field_name = str(field.get("field_name", field.get("selector", "campo"))).strip() or "campo"
                raise NodeExecutionError(f"No se pudo llenar '{field_name}': {exc}") from exc

        if completed == 0:
            raise NodeExecutionError("Nodo form no pudo completar ningun campo")

        clicked_submit = False
        if fill_mode == "ai":
            clicked_submit = await self._apply_submit_action_from_ai(
                page=ctx.page,
                submit_action=submit_action,
                log_fn=ctx.log,
                submit_wait_ms=int(config.get("submit_wait_ms", 2000) or 0),
                success_text=str(config.get("submit_success_text", "")).strip(),
                error_text=str(config.get("submit_error_text", "")).strip(),
            )
        
        return {
            "fields_completed": completed,
            "submit_clicked": clicked_submit,
            "submit_action": submit_action,
        }

    async def _apply_submit_action_from_ai(
        self,
        *,
        page: Page,
        submit_action: dict[str, Any],
        log_fn,
        submit_wait_ms: int = 2000,
        success_text: str = "",
        error_text: str = "",
    ) -> bool:
        if not isinstance(submit_action, dict):
            return False

        action_text = str(submit_action.get("text", "")).strip()
        action_intent = str(submit_action.get("intent", "none")).strip().lower()

        if action_intent == "none" and not action_text:
            log_fn("[WF] IA no indicó acción final de submit para este formulario")
            return False

        candidates: list[str] = []
        if action_text:
            candidates.append(action_text)

        intent_map = {
            "save": ["Guardar", "Continuar", "Enviar", "Finalizar", "Confirmar", "Aceptar"],
            "continue": ["Continuar", "Siguiente", "Guardar", "Enviar", "Aceptar"],
            "submit": ["Enviar", "Guardar", "Continuar", "Finalizar", "Confirmar"],
            "next": ["Siguiente", "Continuar", "Guardar"],
            "finish": ["Finalizar", "Terminar", "Confirmar", "Guardar"],
            "confirm": ["Confirmar", "Aceptar", "Guardar", "Continuar"],
        }
        candidates.extend(intent_map.get(action_intent, []))

        # Evita reintentos duplicados por mayusculas/minusculas.
        deduped_candidates: list[str] = []
        seen_norm: set[str] = set()
        for candidate in candidates:
            cleaned = str(candidate).strip()
            if not cleaned:
                continue
            normalized = cleaned.lower()
            if normalized in seen_norm:
                continue
            seen_norm.add(normalized)
            deduped_candidates.append(cleaned)

        for candidate in deduped_candidates:
            clicked = await self._click_action_candidate(page, candidate)
            if clicked:
                log_fn(f"[WF] Acción final ejecutada por IA: {candidate!r} (intent={action_intent})")

                # Espera configurable para permitir que la UI renderice el resultado del submit.
                if submit_wait_ms > 0:
                    await page.wait_for_timeout(submit_wait_ms)

                if error_text:
                    error_locator = page.get_by_text(re.compile(re.escape(error_text), re.IGNORECASE))
                    try:
                        if await error_locator.count() > 0:
                            raise NodeExecutionError(
                                f"Se detectó mensaje de error tras submit: {error_text}"
                            )
                    except NodeExecutionError:
                        raise
                    except Exception:
                        pass

                if success_text:
                    ok_locator = page.get_by_text(re.compile(re.escape(success_text), re.IGNORECASE))
                    try:
                        if await ok_locator.count() <= 0:
                            raise NodeExecutionError(
                                f"No se detectó confirmación esperada tras submit: {success_text}"
                            )
                    except NodeExecutionError:
                        raise
                    except Exception:
                        pass

                return True

        visible_buttons = await self._collect_visible_button_texts(page)
        raise NodeExecutionError(
            "IA solicitó acción final pero no se encontró un botón compatible. "
            f"intent={action_intent!r} text={action_text!r} visibles={visible_buttons}"
        )

    async def _click_action_candidate(self, page: Page, candidate: str) -> bool:
        exact_pattern = re.compile(rf"^\\s*{re.escape(candidate)}\\s*$", re.IGNORECASE)
        contains_pattern = re.compile(re.escape(candidate), re.IGNORECASE)

        locators = [
            page.locator("form").get_by_role("button", name=exact_pattern),
            page.get_by_role("button", name=exact_pattern),
            page.locator("form").get_by_role("button", name=contains_pattern),
            page.get_by_role("button", name=contains_pattern),
            page.locator("form").get_by_text(exact_pattern),
            page.get_by_text(exact_pattern),
        ]

        for locator in locators:
            try:
                if await locator.count() <= 0:
                    continue
                target = locator.first
                await target.wait_for(state="visible", timeout=2500)
                await target.click(timeout=5000)
                return True
            except Exception:
                continue

        return False

    async def _collect_visible_button_texts(self, page: Page) -> list[str]:
        try:
            values = await page.evaluate(
                """
                () => {
                    const compact = (text) => (text || '').replace(/\s+/g, ' ').trim();
                    const isVisible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                    };

                    const nodes = Array.from(document.querySelectorAll('button, input[type="submit"], input[type="button"]'))
                        .filter((el) => isVisible(el))
                        .map((el) => compact(el.textContent || el.value || el.getAttribute('aria-label') || ''))
                        .filter(Boolean);

                    return Array.from(new Set(nodes)).slice(0, 20);
                }
                """
            )
            return values if isinstance(values, list) else []
        except Exception:
            return []

    async def _capture_base_form_html_context(self, page: Page) -> str:
        """Captura el HTML del formulario + portales visibles ya presentes en DOM,
        limpiando atributos de ruido para reducir tokens en el prompt.
        """
        try:
            result = await page.evaluate("""
            () => {
                const KEEP_ATTRS = new Set([
                    'id','name','for','type','role','aria-label','aria-labelledby',
                    'aria-describedby','aria-expanded','aria-haspopup','aria-required',
                    'placeholder','value','selected','disabled','required','multiple',
                    'data-value',
                    'action','method','enctype'
                ]);

                function cleanNode(el) {
                    const clone = el.cloneNode(true);
                    ['script','style','svg','noscript','iframe','link','meta'].forEach(tag => {
                        clone.querySelectorAll(tag).forEach(n => n.remove());
                    });
                    clone.querySelectorAll('*').forEach(n => {
                        const toRemove = [];
                        for (const attr of n.attributes) {
                            const nm = attr.name.toLowerCase();
                            if (!KEEP_ATTRS.has(nm)) toRemove.push(attr.name);
                        }
                        toRemove.forEach(a => n.removeAttribute(a));
                    });
                    return clone.outerHTML;
                }

                const form = document.querySelector('form');
                const formHtml = form ? cleanNode(form) : '';

                // Portales fuera del form: listboxes (MUI, Ant Design, etc.) y selects nativos
                const portalParts = [];
                document.querySelectorAll('[role="listbox"]').forEach(lb => {
                    if (!form || !form.contains(lb)) {
                        portalParts.push(cleanNode(lb));
                    }
                });
                document.querySelectorAll('select').forEach(sel => {
                    if (!form || !form.contains(sel)) {
                        portalParts.push(sel.outerHTML);
                    }
                });

                return { form: formHtml, portals: portalParts.join('\\n') };
            }
            """)
        except Exception:
            result = None

        if isinstance(result, dict):
            form_html = str(result.get("form") or "")[:90000]
            portals = str(result.get("portals") or "")
            if form_html.strip():
                combined = form_html
                if portals.strip():
                    combined += f"\n<!-- catalogos-portal -->\n{portals[:30000]}"
                return combined[:120000]

        # Fallback: página completa con limpieza Python mínima
        try:
            content = await page.content()
            return self._strip_html_noise(content)[:120000]
        except Exception:
            return ""

    async def _capture_catalog_entries(self, page: Page, log_fn=None) -> list[dict[str, str]]:
        native_entries = await self._capture_native_select_catalog_entries(page)
        live_entries = await self._capture_live_combo_catalog_entries(page, log_fn)
        visible_listbox_entries = await self._capture_visible_listbox_entries(page)
        #pdb.set_trace()
        deduped: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in [*native_entries, *live_entries, *visible_listbox_entries]:
            field_label = str(item.get("field_label", "")).strip()
            catalog_html = str(item.get("catalog_html", "")).strip()
            if not field_label or not catalog_html:
                continue
            key = (field_label.lower(), catalog_html)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(
                {
                    "field_label": field_label,
                    "trigger_html": str(item.get("trigger_html", "")).strip(),
                    "catalog_html": catalog_html,
                    "source": str(item.get("source", "")).strip(),
                }
            )
        if log_fn:
            log_fn(
                "[WF] Catálogos capturados | "
                f"native={len(native_entries)} live={len(live_entries)} visible_listbox={len(visible_listbox_entries)} total={len(deduped)}"
            )
        return deduped

    async def _capture_visible_listbox_entries(self, page: Page) -> list[dict[str, str]]:
        """Fallback: captura listboxes visibles ya renderizados en DOM."""
        try:
            entries = await page.evaluate(
                """
                () => {
                    const KEEP_ATTRS = new Set([
                        'id','name','for','type','role','aria-label','aria-labelledby',
                        'aria-describedby','aria-expanded','aria-haspopup','aria-required',
                        'placeholder','value','selected','disabled','required','multiple',
                        'data-value',
                    ]);
                    const compact = (text) => (text || '').replace(/\s+/g, ' ').trim();
                    const fromIds = (value) => {
                        if (!value) return '';
                        return compact(value.split(/\s+/).map((id) => document.getElementById(id)?.textContent || '').join(' '));
                    };
                    const isVisible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                    };
                    const cleanNode = (el) => {
                        const clone = el.cloneNode(true);
                        ['script','style','svg','noscript','iframe','link','meta'].forEach((tag) => {
                            clone.querySelectorAll(tag).forEach((node) => node.remove());
                        });
                        clone.querySelectorAll('*').forEach((node) => {
                            const toRemove = [];
                            for (const attr of node.attributes) {
                                const name = attr.name.toLowerCase();
                                if (!KEEP_ATTRS.has(name)) toRemove.push(attr.name);
                            }
                            toRemove.forEach((attr) => node.removeAttribute(attr));
                        });
                        return clone.outerHTML;
                    };

                    const listboxes = Array.from(document.querySelectorAll('[role="listbox"]')).filter((el) => {
                        if (!isVisible(el)) return false;
                        return el.querySelector('[role="option"], li, option, div[role="option"]') !== null;
                    });

                    return listboxes.map((lb, idx) => {
                        const labelledText = fromIds(lb.getAttribute('aria-labelledby'));
                        const label = compact(lb.getAttribute('aria-label'))
                            || labelledText
                            || compact(lb.getAttribute('id'))
                            || `listbox_${idx + 1}`;
                        return {
                            field_label: label,
                            trigger_html: '',
                            catalog_html: cleanNode(lb),
                            source: 'visible-listbox',
                        };
                    });
                }
                """
            )
        except Exception:
            entries = []
        return entries if isinstance(entries, list) else []

    async def _capture_native_select_catalog_entries(self, page: Page) -> list[dict[str, str]]:
        try:
            entries = await page.evaluate("""
            () => {
                const form = document.querySelector('form');
                if (!form) return [];
                const KEEP_ATTRS = new Set([
                    'id','name','for','type','role','aria-label','aria-labelledby',
                    'aria-describedby','aria-expanded','aria-haspopup','aria-required',
                    'placeholder','value','selected','disabled','required','multiple',
                    'data-value',
                ]);
                const compact = (text) => (text || '').replace(/\s+/g, ' ').trim();
                const isVisible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                };
                const resolveLabel = (el) => {
                    const fromIds = (value) => {
                        if (!value) return '';
                        return compact(value.split(/\s+/).map((id) => document.getElementById(id)?.textContent || '').join(' '));
                    };
                    const ariaLabel = compact(el.getAttribute('aria-label'));
                    if (ariaLabel) return ariaLabel;
                    const labelled = fromIds(el.getAttribute('aria-labelledby'));
                    if (labelled) return labelled;
                    const id = el.getAttribute('id');
                    if (id) {
                        const labelEl = document.querySelector(`label[for="${id}"]`);
                        const labelText = compact(labelEl?.textContent || '');
                        if (labelText) return labelText;
                    }
                    const formControl = el.closest('.MuiFormControl-root, .MuiGrid-root, .MuiStack-root, .MuiBox-root, label, fieldset, [data-field], [data-testid]');
                    const nearbyLabel = formControl?.querySelector('label, .MuiInputLabel-root, [id$="-label"], [data-field-label]');
                    return compact(nearbyLabel?.textContent || el.getAttribute('name') || 'select');
                };
                const cleanNode = (el, label) => {
                    const clone = el.cloneNode(true);
                    ['script','style','svg','noscript','iframe','link','meta'].forEach((tag) => {
                        clone.querySelectorAll(tag).forEach((node) => node.remove());
                    });
                    clone.querySelectorAll('*').forEach((node) => {
                        const toRemove = [];
                        for (const attr of node.attributes) {
                            const name = attr.name.toLowerCase();
                            if (!KEEP_ATTRS.has(name)) toRemove.push(attr.name);
                        }
                        toRemove.forEach((attr) => node.removeAttribute(attr));
                    });
                    if (label && !clone.getAttribute('aria-label')) {
                        clone.setAttribute('aria-label', label);
                    }
                    return clone.outerHTML;
                };

                return Array.from(form.querySelectorAll('select')).filter(isVisible).map((el) => {
                    const label = resolveLabel(el);
                    return {
                        field_label: label || 'select',
                        trigger_html: cleanNode(el, label),
                        catalog_html: cleanNode(el, label),
                        source: 'native-select',
                    };
                });
            }
            """)
        except Exception:
            entries = []
        return entries if isinstance(entries, list) else []

    async def _capture_live_combo_catalog_entries(self, page: Page, log_fn=None) -> list[dict[str, str]]:
        """Abre cada combo visible y captura solo el HTML del catálogo expandido."""
        entries: list[dict[str, str]] = []
        processed_signatures: set[str] = set()
        max_iterations = 40

        if log_fn:
            log_fn("[WF] Explorando combos dinámicos (redescubriendo elementos tras cada re-render)")

        for index in range(1, max_iterations + 1):
            try:
                metadata = await page.evaluate(
                    """
                    (processed) => {
                        document.querySelectorAll('[data-aiqa-active-candidate]').forEach((el) => {
                            el.removeAttribute('data-aiqa-active-candidate');
                        });

                        const selector = [
                            'div.MuiSelect-select[role="combobox"]',
                            '[role="combobox"]',
                            'button[aria-haspopup="listbox"]',
                            'div[aria-haspopup="listbox"]',
                            'span[aria-haspopup="listbox"]',
                            'input[role="combobox"]',
                            '.MuiSelect-select',
                        ].join(',');
                        const seen = new Set(Array.isArray(processed) ? processed : []);
                        const compact = (text) => (text || '').replace(/\s+/g, ' ').trim();
                        const isVisible = (el) => {
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                        };
                        const fromIds = (value) => {
                            if (!value) return '';
                            return compact(
                                value.split(/\s+/).map((id) => document.getElementById(id)?.textContent || '').join(' ')
                            );
                        };
                        const resolveLabel = (el) => {
                            const ariaLabel = compact(el.getAttribute('aria-label'));
                            if (ariaLabel) return ariaLabel;
                            const labelled = fromIds(el.getAttribute('aria-labelledby'));
                            if (labelled) return labelled;
                            const id = el.getAttribute('id');
                            if (id) {
                                const labelEl = document.querySelector(`label[for="${id}"]`);
                                const labelText = compact(labelEl?.textContent || '');
                                if (labelText) return labelText;
                            }
                            const formControl = el.closest('.MuiFormControl-root, .MuiGrid-root, .MuiStack-root, .MuiBox-root, label, fieldset, [data-field], [data-testid]');
                            const nearbyLabel = formControl?.querySelector('label, .MuiInputLabel-root, [id$="-label"], [data-field-label]');
                            const nearbyText = compact(nearbyLabel?.textContent || '');
                            if (nearbyText) return nearbyText;
                            return compact(el.textContent || el.getAttribute('name') || el.getAttribute('placeholder') || 'combo');
                        };
                        const signatureOf = (el) => {
                            const parts = [
                                compact(el.getAttribute('id')),
                                compact(el.getAttribute('name')),
                                compact(el.getAttribute('aria-label')),
                                compact(el.getAttribute('aria-labelledby')),
                                compact(el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || ''),
                                compact(el.getAttribute('data-testid')),
                                compact(el.className || ''),
                                compact((el.textContent || '').slice(0, 80)),
                            ];
                            return parts.filter(Boolean).join('|').toLowerCase();
                        };

                        const candidates = Array.from(document.querySelectorAll(selector)).filter((el) => {
                            if (!isVisible(el)) return false;
                            if (el.closest('[role="listbox"]')) return false;
                            if (el.getAttribute('aria-disabled') === 'true') return false;
                            return true;
                        });

                        for (const el of candidates) {
                            const signature = signatureOf(el);
                            if (!signature || seen.has(signature)) {
                                continue;
                            }
                            el.setAttribute('data-aiqa-active-candidate', '1');
                            return {
                                found: true,
                                remaining_count: candidates.length,
                                signature,
                                field_label: resolveLabel(el),
                                trigger_html: el.outerHTML,
                                listbox_id: el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '',
                                labelled_by: el.getAttribute('aria-labelledby') || '',
                            };
                        }

                        return {
                            found: false,
                            remaining_count: candidates.length,
                            signature: '',
                            field_label: '',
                            trigger_html: '',
                            listbox_id: '',
                            labelled_by: '',
                        };
                    }
                    """,
                    list(processed_signatures),
                )
            except Exception as exc:
                if log_fn:
                    log_fn(f"[WF] No se pudieron identificar combos para catálogos dinámicos: {exc}")
                break
            #pdb.set_trace()
            if not isinstance(metadata, dict) or not bool(metadata.get("found")):
                break

            signature = str(metadata.get("signature") or "").strip().lower()
            label = str(metadata.get("field_label") or f"combo_{index}")
            trigger_html = self._strip_html_noise(str(metadata.get("trigger_html") or ""))
            listbox_id = str(metadata.get("listbox_id") or "").strip()
            labelled_by = str(metadata.get("labelled_by") or "").strip()
            #import pdb; pdb.set_trace()
            if not signature:
                signature = f"fallback|{label.strip().lower()}|{index}"
            processed_signatures.add(signature)

            locator = page.locator('[data-aiqa-active-candidate="1"]').first
            try:
                await locator.wait_for(state="visible", timeout=1500)
            except Exception:
                continue

            try:
                await locator.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass

            try:
                await locator.click(timeout=3000, force=True)
            except Exception:
                try:
                    await locator.press("ArrowDown")
                except Exception as exc:
                    if log_fn:
                        log_fn(f"[WF] No se pudo abrir combo '{label}': {exc}")
                    continue

            try:
                await page.wait_for_function(
                    """
                    ({listboxId, labelledBy}) => {
                        const visible = (el) => {
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none'
                                && style.visibility !== 'hidden'
                                && rect.width > 0
                                && rect.height > 0;
                        };

                        if (listboxId) {
                            const byId = document.getElementById(listboxId);
                            if (byId && visible(byId)) return true;
                        }

                        const labelledTokens = (labelledBy || '')
                            .split(/\\s+/)
                            .map(v => v.trim())
                            .filter(Boolean);

                        if (labelledTokens.length > 0) {
                            const byLabel = Array.from(
                                document.querySelectorAll('[role="listbox"]')
                            ).find((lb) => {
                                if (!visible(lb)) return false;

                                const lbTokens = (lb.getAttribute('aria-labelledby') || '')
                                    .split(/\\s+/)
                                    .map(v => v.trim())
                                    .filter(Boolean);

                                return labelledTokens.some(
                                    token => lbTokens.includes(token)
                                );
                            });

                            if (byLabel) return true;
                        }

                        return false;
                    }
                    """,
                    arg={
                        "listboxId": listbox_id,
                        "labelledBy": labelled_by,
                    },
                    timeout=3000,
                )
            except Exception:
                pass

            try:
               catalog_html = await page.evaluate(
                    """
                    ({comboLabel, listboxId, labelledBy}) => {
                        const KEEP_ATTRS = new Set([
                            'id','name','for','type','role','aria-label','aria-labelledby',
                            'aria-describedby','aria-expanded','aria-haspopup','aria-required',
                            'placeholder','value','selected','disabled','required','multiple',
                            'data-value',
                        ]);

                        const compact = (text) => (text || '').replace(/\\s+/g, ' ').trim();

                        const isVisible = (el) => {
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return (
                                style.display !== 'none' &&
                                style.visibility !== 'hidden' &&
                                rect.width > 0 &&
                                rect.height > 0
                            );
                        };

                        const cleanNode = (el) => {
                            const clone = el.cloneNode(true);

                            ['script','style','svg','noscript','iframe','link','meta']
                                .forEach((tag) => {
                                    clone.querySelectorAll(tag)
                                        .forEach((node) => node.remove());
                                });

                            clone.querySelectorAll('*').forEach((node) => {
                                const toRemove = [];

                                for (const attr of node.attributes) {
                                    const name = attr.name.toLowerCase();

                                    if (!KEEP_ATTRS.has(name)) {
                                        toRemove.push(attr.name);
                                    }
                                }

                                toRemove.forEach((attr) => {
                                    node.removeAttribute(attr);
                                });
                            });

                            if (comboLabel && !clone.getAttribute('aria-label')) {
                                clone.setAttribute('aria-label', compact(comboLabel));
                            }

                            return clone.outerHTML;
                        };

                        const form = document.querySelector('form');

                        if (listboxId) {
                            const listbox = document.getElementById(listboxId);

                            if (listbox && isVisible(listbox)) {
                                return cleanNode(listbox);
                            }
                        }

                        if (labelledBy) {
                            const labelledListbox = Array.from(
                                document.querySelectorAll('[role="listbox"]')
                            ).find((el) => {
                                if (!isVisible(el)) {
                                    return false;
                                }

                                const lbTokens = (
                                    el.getAttribute('aria-labelledby') || ''
                                )
                                    .split(/\\s+/)
                                    .map((v) => v.trim())
                                    .filter(Boolean);

                                const comboTokens = (
                                    labelledBy || ''
                                )
                                    .split(/\\s+/)
                                    .map((v) => v.trim())
                                    .filter(Boolean);

                                return comboTokens.some((token) =>
                                    lbTokens.includes(token)
                                );
                            });

                            if (labelledListbox) {
                                return cleanNode(labelledListbox);
                            }
                        }

                        const visibleListboxes = Array.from(
                            document.querySelectorAll('[role="listbox"]')
                        ).filter((el) => {
                            return isVisible(el) &&
                                (!form || !form.contains(el));
                        });

                        if (visibleListboxes.length > 0) {
                            return visibleListboxes
                                .map(cleanNode)
                                .join('\\n');
                        }

                        const visibleSelects = Array.from(
                            document.querySelectorAll('select')
                        ).filter((el) => {
                            return isVisible(el) &&
                                (!form || !form.contains(el));
                        });

                        if (visibleSelects.length > 0) {
                            return visibleSelects
                                .map(cleanNode)
                                .join('\\n');
                        }

                        const optionNodes = Array.from(
                            document.querySelectorAll(
                                '[role="option"], ul > li, div[role] > div, div[role] > span'
                            )
                        ).filter(isVisible);

                        if (optionNodes.length > 0) {
                            const optionHtml = optionNodes
                                .map((el) => cleanNode(el))
                                .join('');

                            const safeLabel = compact(comboLabel || 'combo');

                            return `<div role="group" aria-label="${safeLabel}">${optionHtml}</div>`;
                        }

                        return '';
                    }
                    """,
                    {
                        "comboLabel": label,
                        "listboxId": listbox_id,
                        "labelledBy": labelled_by,
                    },
                )
               
            except Exception as exc:
                if log_fn:
                    log_fn(f"[WF] No se pudo leer catálogo del combo '{label}': {exc}")
                catalog_html = ""
            if isinstance(catalog_html, str) and catalog_html.strip():
                options = self._extract_options_from_catalog_html(catalog_html)
                option_count = len(options)
                option_preview = ", ".join(options[:5]) if options else "Sin opciones detectadas"
                entries.append(
                    {
                        "field_label": label,
                        "trigger_html": trigger_html,
                        "catalog_html": catalog_html,
                        "source": "expanded-combo",
                    }
                )
                if log_fn:
                    log_fn(
                        f"[WF] Catálogo capturado para combo '{label}' | "
                        f"opciones_detectadas={option_count} | preview=[{option_preview}]"
                    )

            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

        try:
            await page.evaluate(
                """
                () => {
                    document.querySelectorAll('[data-aiqa-active-candidate]').forEach((el) => {
                        el.removeAttribute('data-aiqa-active-candidate');
                    });
                }
                """
            )
        except Exception:
            pass

        return entries

    @staticmethod
    def _strip_html_noise(html: str) -> str:
        """Limpieza Python de emergencia: elimina bloques script/style/svg y comentarios."""
        import re as _re
        for tag in ("script", "style", "svg", "noscript", "iframe", "link", "meta"):
            html = _re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", "", html, flags=_re.IGNORECASE | _re.DOTALL)
        html = _re.sub(r"<!--.*?-->", "", html, flags=_re.DOTALL)
        html = _re.sub(r"\n{3,}", "\n\n", html)
        return html.strip()

    @staticmethod
    def _extract_options_from_catalog_html(catalog_html: str) -> list[str]:
        option_patterns = [
            re.compile(r"<option\b[^>]*>(?P<body>.*?)</option>", re.IGNORECASE | re.DOTALL),
            re.compile(
                r'<(?P<tag>li|div|span|tr)\b(?P<attrs>[^>]*)role=["\']option["\'][^>]*>(?P<body>.*?)</(?P=tag)>',
                re.IGNORECASE | re.DOTALL,
            ),
            re.compile(r"<li\b[^>]*>(?P<body>.*?)</li>", re.IGNORECASE | re.DOTALL),
            re.compile(r"<div\b[^>]*>(?P<body>.*?)</div>", re.IGNORECASE | re.DOTALL),
        ]

        options: list[str] = []
        for pattern in option_patterns:
            for match in pattern.finditer(catalog_html or ""):
                attrs = match.groupdict().get("attrs", "")
                body = match.groupdict().get("body", "")
                value = FillFormNodeExecutor._extract_role_option_value(attrs, body)
                if value:
                    options.append(value)
            if options:
                break

        return list(dict.fromkeys(option for option in options if option))[:50]

    @staticmethod
    def _extract_role_option_value(attrs: str, body: str) -> str:
        data_value_match = re.search(r'data-value=["\']([^"\']+)["\']', attrs or "", re.IGNORECASE)
        if data_value_match:
            return FillFormNodeExecutor._clean_catalog_text(data_value_match.group(1))
        return FillFormNodeExecutor._clean_catalog_text(body)

    @staticmethod
    def _clean_catalog_text(raw_text: str) -> str:
        text = re.sub(r"<[^>]+>", " ", raw_text or "")
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _resolve_field_locator(self, page: Page, field: dict[str, Any]) -> Any | None:
        candidates = self._build_field_locators(page, field)
        return candidates[0] if candidates else None

    async def _build_form_field_name_index(self, page: Page, log_fn=None) -> dict[str, list[str]]:
        """Construye un índice para mapear label/placeholder -> name real del control.

        Esto permite resolver campos cuando el DOM usa names numéricos (ej. name='13087').
        """
        try:
            entries = await page.evaluate(
                """
                () => {
                    const compact = (text) => (text || '').replace(/\s+/g, ' ').trim();
                    const isVisible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                    };
                    const fromIds = (value) => {
                        if (!value) return '';
                        return compact(value.split(/\s+/).map((id) => document.getElementById(id)?.textContent || '').join(' '));
                    };
                    const resolveLabel = (el) => {
                        const ariaLabel = compact(el.getAttribute('aria-label'));
                        if (ariaLabel) return ariaLabel;

                        const labelled = fromIds(el.getAttribute('aria-labelledby'));
                        if (labelled) return labelled;

                        const id = compact(el.getAttribute('id'));
                        if (id) {
                            const labelEl = document.querySelector(`label[for="${id}"]`);
                            const labelText = compact(labelEl?.textContent || '');
                            if (labelText) return labelText;
                        }

                        const formControl = el.closest('.MuiFormControl-root, .MuiGrid-root, .MuiStack-root, .MuiBox-root, label, fieldset, [data-field], [data-testid]');
                        const nearbyLabel = formControl?.querySelector('label, .MuiInputLabel-root, [id$="-label"], [data-field-label]');
                        return compact(nearbyLabel?.textContent || '');
                    };

                    const selector = 'input, textarea, select, [role="combobox"], [contenteditable="true"]';
                    const controls = Array.from(document.querySelectorAll(selector)).filter((el) => isVisible(el));
                    const mapped = [];

                    for (const el of controls) {
                        const name = compact(el.getAttribute('name'));
                        if (!name) continue;

                        const label = resolveLabel(el);
                        const placeholder = compact(el.getAttribute('placeholder'));
                        const id = compact(el.getAttribute('id'));

                        mapped.push({ label, placeholder, id, name });
                    }

                    return mapped;
                }
                """
            )
        except Exception:
            entries = []

        index: dict[str, set[str]] = {}
        for item in entries if isinstance(entries, list) else []:
            if not isinstance(item, dict):
                continue

            name = str(item.get("name", "")).strip()
            if not name:
                continue

            label = str(item.get("label", "")).strip()
            placeholder = str(item.get("placeholder", "")).strip()
            raw_keys = [label, placeholder]
            if self._looks_like_date_placeholder(placeholder):
                # Sinónimos para facilitar resolución de campos fecha por label o placeholder.
                raw_keys.extend([
                    "dd mm aaaa",
                    "dd/mm/aaaa",
                    "fecha",
                    "fecha escritura",
                    "fecha de escritura",
                ])
            for raw_key in raw_keys:
                normalized_key = self._normalize_lookup_key(raw_key)
                if not normalized_key:
                    continue
                index.setdefault(normalized_key, set()).add(name)

        result = {key: sorted(values) for key, values in index.items()}
        if log_fn:
            preview_pairs: list[str] = []
            for item in entries if isinstance(entries, list) else []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                label = str(item.get("label", "")).strip()
                placeholder = str(item.get("placeholder", "")).strip()
                key_text = label or placeholder
                if not key_text:
                    continue
                preview_pairs.append(f"{key_text}->{name}")

            # Log compacto para depurar que campos como DD/MM/AAAA se asocien a su name real.
            unique_preview_pairs = list(dict.fromkeys(preview_pairs))
            preview_text = ", ".join(unique_preview_pairs[:12]) if unique_preview_pairs else "sin-pares"
            log_fn(
                "[WF] Índice label/placeholder->name construido: "
                f"{len(result)} clave(s) | preview=[{preview_text}]"
            )
        return result

    def _build_field_locators(
        self,
        page: Page,
        field: dict[str, Any],
        *,
        field_name_index: dict[str, list[str]] | None = None,
    ) -> list[Any]:
        selector_type = str(field.get("selector_type", "label")).strip().lower()
        selector = str(field.get("selector", "")).strip()
        field_name = str(field.get("field_name", "")).strip()
        target = selector or field_name

        if not target:
            return []

        exact_pattern = re.compile(rf"^\\s*{re.escape(target)}\\s*$", re.I)
        contains_pattern = re.compile(re.escape(target), re.I)
        key_candidates = self._build_field_key_candidates(target)
        raw_lookup_candidates = [target, field_name, selector, *key_candidates]
        mapped_name_candidates: list[str] = []
        if field_name_index:
            seen_names: set[str] = set()
            for raw_key in raw_lookup_candidates:
                normalized_key = self._normalize_lookup_key(raw_key)
                if not normalized_key:
                    continue
                for mapped_name in field_name_index.get(normalized_key, []):
                    clean_name = str(mapped_name).strip()
                    if not clean_name or clean_name in seen_names:
                        continue
                    seen_names.add(clean_name)
                    mapped_name_candidates.append(clean_name)

        def unique(items: list[Any]) -> list[Any]:
            deduped: list[Any] = []
            seen: set[str] = set()
            for item in items:
                marker = str(item)
                if marker in seen:
                    continue
                seen.add(marker)
                deduped.append(item)
            return deduped

        if selector_type in {"css", "xpath"}:
            query = target if selector_type == "css" or target.startswith("xpath=") else f"xpath={target}"
            return [page.locator(query).first]

        if selector_type == "name":
            candidates: list[Any] = [page.locator(f"[name={json.dumps(target)}]")]
            for key in key_candidates:
                candidates.append(page.locator(f"[name={json.dumps(key)}]"))
            for mapped_name in mapped_name_candidates:
                candidates.append(page.locator(f"[name={json.dumps(mapped_name)}]"))
            return unique(candidates)

        if selector_type == "placeholder":
            return unique(
                [
                    page.locator(f"[placeholder={json.dumps(target)}]"),
                    page.get_by_placeholder(target),
                    page.get_by_placeholder(exact_pattern),
                    page.get_by_placeholder(contains_pattern),
                ]
            )

        field_controls = "input, textarea, select, [contenteditable='true']"
        candidates = [
            page.get_by_label(target),
            page.get_by_label(exact_pattern),
            page.get_by_label(contains_pattern),
            page.locator(f"[aria-label={json.dumps(target)}]"),
            page.locator(f"[placeholder={json.dumps(target)}]"),
            page.get_by_placeholder(target),
            page.locator("label").filter(has_text=contains_pattern).locator(field_controls),
            page.locator(f"label:has-text(\"{target}\") + {field_controls}"),
            page.locator(
                "xpath="
                f"//label[contains(normalize-space(.), {json.dumps(target)})]"
                f"/following::*[self::input or self::textarea or self::select][1]"
            ),
            page.locator("label").filter(has_text=contains_pattern).locator("[role='combobox']"),
            page.locator(f"label:has-text(\"{target}\") + [role='combobox']"),
        ]
        
        for key in key_candidates:
            candidates.append(page.locator(f"[name={json.dumps(key)}]"))
            candidates.append(page.locator(f"[id={json.dumps(key)}]"))

        for mapped_name in mapped_name_candidates:
            candidates.append(page.locator(f"[name={json.dumps(mapped_name)}]"))

        return unique(candidates)

    def _normalize_lookup_key(self, value: str) -> str:
        normalized = self._clean_catalog_text(value or "").lower()
        normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    def _looks_like_date_placeholder(self, value: str) -> bool:
        raw = self._clean_catalog_text(value or "").lower()
        normalized = self._normalize_lookup_key(value)
        if not normalized:
            return False

        if normalized in {"dd mm aaaa", "dd mm aa", "aaaa mm dd", "yyyy mm dd"}:
            return True

        compact = re.sub(r"\s+", "", raw)
        return compact in {"dd/mm/aaaa", "dd-mm-aaaa", "aaaa-mm-dd", "yyyy-mm-dd"}

    def _build_field_key_candidates(self, raw_label: str) -> list[str]:
        normalized = self._clean_catalog_text(raw_label or "").lower()
        normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if not normalized:
            return []

        parts = [part for part in normalized.split(" ") if part]
        if not parts:
            return []

        snake = "_".join(parts)
        kebab = "-".join(parts)
        flat = "".join(parts)
        camel = parts[0] + "".join(word.capitalize() for word in parts[1:])

        ordered = [snake, kebab, flat, camel]
        seen: set[str] = set()
        result: list[str] = []
        for item in ordered:
            if not item or item in seen:
                continue
            seen.add(item)
            result.append(item)
        return result

    def _resolve_field_value(self, field: dict[str, Any]) -> str:
        raw_value = str(field.get("value", "")).strip()
        if raw_value:
            return raw_value

        field_name = str(field.get("field_name", "")).strip()
        selector = str(field.get("selector", "")).strip()
        data_type = self._infer_field_data_type(field_name=field_name, selector=selector)
        return self._SAMPLE_VALUES.get(data_type, self._SAMPLE_VALUES["text"])

    def _infer_field_data_type(self, field_name: str, selector: str) -> str:
        hint = f"{field_name} {selector}".lower()

        if any(token in hint for token in ["fecha", "date", "dd/mm/aaaa", "aaaa-mm-dd"]):
            return "date"
        if any(token in hint for token in ["correo", "email", "e-mail"]):
            return "email"
        if any(token in hint for token in ["telefono", "teléfono", "celular", "movil", "móvil"]):
            return "phone"
        if any(token in hint for token in ["regimen fiscal", "rfc", "razon social", "razón social"]):
            return "rfc"
        if any(token in hint for token in ["importe", "monto", "precio", "valor", "total", "costo"]):
            return "currency"
        if any(token in hint for token in ["folio", "numero", "número", "cantidad", "cp", "código postal", "codigo postal"]):
            return "number"
        if any(token in hint for token in ["domicilio", "direccion", "dirección", "calle", "colonia", "municipio"]):
            return "address"
        if any(token in hint for token in ["nombre", "cliente", "persona", "propietario", "beneficiario"]):
            return "name"
        return "text"

    async def _fill_or_select_field(self, page: Page, locator: Any, field: dict[str, Any], value: str, log_fn) -> None:
        """Rellena o selecciona un campo según su tipo real en el DOM.

        - <select> nativo → select_option()
        - <div|span|button role="combobox"> (MUI, Ant Design, etc.) → click + option
        - <input|textarea|[contenteditable]> → fill() normal
        """
        try:
            tag = str(await locator.evaluate("el => el.tagName.toLowerCase()"))
        except Exception:
            tag = ""

        try:
            role = str(await locator.evaluate("el => (el.getAttribute('role') || '').toLowerCase()"))
        except Exception:
            role = ""

        field_hint = " ".join(
            [
                str(field.get("field_name", "")),
                str(field.get("selector", "")),
            ]
        ).strip().lower()
        value_looks_like_date = bool(re.match(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$", str(value).strip()))
        hint_looks_like_date = any(token in field_hint for token in ["fecha", "date", "escritura"])

        try:
            is_contenteditable = bool(await locator.evaluate("el => el.isContentEditable === true"))
        except Exception:
            is_contenteditable = False

        # 1. Select nativo
        if tag == "select":
            await locator.select_option(label=value)
            log_fn(f"[WF] select_option (native select): {value!r}")
            return

        # 2. Inputs reales (incluye date-picker sobre input): priorizar fill sobre lógica combobox.
        if tag in {"input", "textarea"} or is_contenteditable:
            try:
                await locator.fill(value)
                log_fn(f"[WF] fill input/textarea ({role or 'sin-role'}): {value!r}")
                return
            except Exception:
                # Fallback para controles con comportamiento especial (algunos date-pickers)
                await locator.click(timeout=5000)
                if tag == "input":
                    try:
                        await locator.press("Control+a")
                    except Exception:
                        pass
                await locator.type(value, delay=20)
                try:
                    await locator.press("Tab")
                except Exception:
                    pass
                log_fn(f"[WF] type fallback en input/textarea ({role or 'sin-role'}): {value!r}")
                return

        # 3. Combobox custom (MUI Select, Ant Design Select, etc.)
        if role == "combobox" or (tag in {"div", "button", "span"} and role in {"combobox", "listbox", ""}):
            # Intentar primero si hay un <input> interno (algunos combobox wrappean un input real)
            try:
                inner_input = locator.locator("input").first
                if await inner_input.count() > 0:
                    await inner_input.fill(value)
                    log_fn(f"[WF] fill en input interno del combobox: {value!r}")
                    return
            except Exception:
                pass

            # Si por contexto parece fecha, intentar tecleo directo antes de buscar opciones.
            if value_looks_like_date or hint_looks_like_date:
                try:
                    await locator.click(timeout=5000)
                    await page.keyboard.press("Control+a")
                    await page.keyboard.type(value, delay=20)
                    await page.keyboard.press("Tab")
                    log_fn(f"[WF] combobox tratado como input fecha: {value!r}")
                    return
                except Exception:
                    pass

            # Abrir el dropdown
            await locator.click(timeout=5000)
            log_fn(f"[WF] Abriendo combobox para seleccionar: {value!r}")

            # Esperar que aparezca el listbox
            listbox = page.locator('[role="listbox"]').first
            try:
                await listbox.wait_for(state="visible", timeout=5000)
            except Exception:
                pass

            # Buscar la opción por texto (estrategias en orden de confianza)
            option: Any = None
            option_locators = [
                page.get_by_role("option", name=re.compile(re.escape(value), re.I)),
                page.locator(f'[role="option"]:has-text("{value}")'),
                page.locator(f'li[role="option"]:has-text("{value}")'),
                page.locator(f'li:has-text("{value}")').first,
                page.get_by_text(re.compile(re.escape(value), re.I)),
            ]
            for opt in option_locators:
                try:
                    if await opt.count() > 0:
                        option = opt.first
                        break
                except Exception:
                    continue

            if option is None:
                # Fallback final: algunos pseudo-combobox aceptan escritura libre sin opciones visibles.
                try:
                    await locator.click(timeout=5000)
                    await page.keyboard.press("Control+a")
                    await page.keyboard.type(value, delay=20)
                    await page.keyboard.press("Tab")
                    log_fn(f"[WF] fallback escritura en pseudo-combobox: {value!r}")
                    return
                except Exception as input_like_exc:
                    raise NodeExecutionError(
                        f"No se encontró la opción '{value}' en el listbox del combobox"
                    ) from input_like_exc

            await option.wait_for(state="visible", timeout=5000)
            await option.click(timeout=5000)
            log_fn(f"[WF] Opción seleccionada en combobox: {value!r}")
            return

        # 4. Fallback genérico
        await locator.fill(value)
        log_fn(f"[WF] fill normal en {tag or 'elemento'}: {value!r}")


class ValidationNodeExecutor(NodeExecutor):
    node_type = "validation"

    async def execute(self, node: dict[str, Any], ctx: RuntimeContext) -> dict[str, Any]:
        config = _resolve_config(node.get("config", {}), ctx.variables)
        assertion_type = str(config.get("assertion_type", "contains_text")).strip().lower()
        selector = str(config.get("selector", "")).strip()
        expected_text = str(config.get("expected_text", "")).strip()
        wait_ms = int(config.get("wait_ms", ctx.runtime_settings.get("wait_after_navigation_ms", 3000)) or 0)
        timeout_ms = max(500, wait_ms)
        case_sensitive = bool(config.get("case_sensitive", False))

        if assertion_type == "contains_text":
            source_text = ""
            node_label = str(node.get("label", "")).strip()

            if selector:
                locator = ctx.page.locator(selector).first
                try:
                    await locator.wait_for(state="visible", timeout=timeout_ms)
                except Exception as exc:
                    raise NodeExecutionError(
                        f"Validation contains_text no encontró selector visible: {selector}"
                    ) from exc

                source_text = await self._read_locator_text(locator)
                if not source_text:
                    source_text = await self._read_page_text(ctx.page)
            else:
                source_text = await self._read_page_text(ctx.page)

            healed_expected = expected_text
            if not healed_expected and selector:
                healed_expected = source_text.strip()
                if healed_expected:
                    preview = healed_expected[:120].replace("\n", " ")
                    ctx.log(
                        "[WF] Auto-healing validation contains_text: expected_text ausente; "
                        f"se usa texto del selector '{selector}': {preview!r}"
                    )

            if not healed_expected and node_label:
                healed_expected = node_label
                ctx.log(
                    "[WF] Auto-healing validation contains_text: expected_text ausente; "
                    f"se usa label del nodo: {node_label!r}"
                )

            if not healed_expected:
                raise NodeExecutionError(
                    "Validation contains_text requiere expected_text, selector o label de nodo"
                )

            normalized_expected = self._normalize_text_for_compare(healed_expected, case_sensitive=case_sensitive)
            normalized_source = self._normalize_text_for_compare(source_text, case_sensitive=case_sensitive)

            if normalized_expected not in normalized_source:
                preview = source_text[:300].replace("\n", " ")
                raise NodeExecutionError(
                    "Texto esperado no encontrado. "
                    f"expected={healed_expected!r} selector={selector!r} "
                    f"case_sensitive={case_sensitive} texto_actual={preview!r}"
                )

            return {
                "assertion": "contains_text",
                "ok": True,
                "expected_text": healed_expected,
                "selector": selector,
            }

        if assertion_type == "is_visible":
            if not selector:
                raise NodeExecutionError("Validation is_visible requiere selector")
            locator = ctx.page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=timeout_ms)
                visible = True
            except Exception:
                visible = False
            if not visible:
                raise NodeExecutionError(f"Elemento no visible: {selector}")
            return {"assertion": "is_visible", "ok": True}

        if assertion_type == "url_contains":
            if not expected_text:
                raise NodeExecutionError("Validation url_contains requiere expected_text")
            if expected_text not in ctx.page.url:
                raise NodeExecutionError(f"URL actual no contiene '{expected_text}': {ctx.page.url}")
            return {"assertion": "url_contains", "ok": True, "url": ctx.page.url}

        raise NodeExecutionError(f"Tipo de validacion no soportado: {assertion_type}")

    async def _read_page_text(self, page: Page) -> str:
        try:
            return await page.evaluate(
                """
                () => {
                    const body = document.body;
                    if (!body) return '';
                    return (body.innerText || '').replace(/\s+/g, ' ').trim();
                }
                """
            )
        except Exception:
            html = await page.content()
            return html or ""

    async def _read_locator_text(self, locator) -> str:
        try:
            text = await locator.inner_text(timeout=3000)
            return re.sub(r"\s+", " ", text or "").strip()
        except Exception:
            try:
                text = await locator.text_content(timeout=3000)
                return re.sub(r"\s+", " ", text or "").strip()
            except Exception:
                return ""

    def _normalize_text_for_compare(self, text: str, *, case_sensitive: bool) -> str:
        compact = re.sub(r"\s+", " ", text or "").strip()
        if case_sensitive:
            return compact
        return compact.lower()


class UploadNodeExecutor(NodeExecutor):
    node_type = "upload"

    async def execute(self, node: dict[str, Any], ctx: RuntimeContext) -> dict[str, Any]:
        config = _resolve_config(node.get("config", {}), ctx.variables)
        legacy_selector = str(config.get("selector", "")).strip()
        upload_selector = str(config.get("upload_selector", "")).strip()
        upload_selector_type = str(config.get("upload_selector_type", "")).strip().lower()
        selector = upload_selector or legacy_selector
        file_path = str(config.get("file_path", "")).strip()
        upload_file_name = str(config.get("upload_file_name", "")).strip() or "upload.bin"
        upload_file_base64 = str(config.get("upload_file_base64", "")).strip()

        if not selector:
            raise NodeExecutionError("Nodo upload requiere 'selector' o 'upload_selector'")

        if not upload_selector_type:
            upload_selector_type = "label" if upload_selector else "css"

        locator = self._resolve_upload_locator(ctx.page, upload_selector_type, selector)
        if locator is None:
            raise NodeExecutionError("Nodo upload no pudo resolver el componente de carga")

        resolved_path: str
        if file_path:
            if not Path(file_path).exists():
                raise NodeExecutionError(f"Archivo no encontrado para upload: {file_path}")
            resolved_path = file_path
        elif upload_file_base64:
            resolved_path = self._materialize_upload_file(
                content_base64=upload_file_base64,
                file_name=upload_file_name,
                ctx=ctx,
            )
        else:
            raise NodeExecutionError("Nodo upload requiere 'file_path' o 'upload_file_base64'")

        await locator.set_input_files(resolved_path)
        return {
            "uploaded": resolved_path,
            "selector": selector,
            "selector_type": upload_selector_type,
        }

    def _resolve_upload_locator(self, page: Page, selector_type: str, selector: str) -> Any | None:
        target = str(selector).strip()
        if not target:
            return None

        if selector_type in {"css", "xpath"}:
            query = target if selector_type == "css" or target.startswith("xpath=") else f"xpath={target}"
            return page.locator(query).first

        if selector_type == "name":
            query = f"[name={json.dumps(target)}]"
            return page.locator(query).first

        if selector_type == "placeholder":
            query = f"[placeholder={json.dumps(target)}]"
            return page.locator(query).first

        return page.get_by_label(re.compile(re.escape(target), re.I)).first

    def _materialize_upload_file(self, *, content_base64: str, file_name: str, ctx: RuntimeContext) -> str:
        safe_name = _safe_file_name(file_name or "upload.bin")
        base_dir = ctx.artifacts_dir if ctx.artifacts_dir else Path("playwright-artifacts")
        uploads_dir = base_dir / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)

        output_path = uploads_dir / safe_name
        try:
            decoded = base64.b64decode(content_base64, validate=True)
        except binascii.Error as exc:
            raise NodeExecutionError("upload_file_base64 inválido") from exc

        output_path.write_bytes(decoded)
        ctx.log(f"[WF] Archivo materializado para upload: {output_path}")
        return str(output_path)


class WaitNodeExecutor(NodeExecutor):
    node_type = "wait"

    async def execute(self, node: dict[str, Any], ctx: RuntimeContext) -> dict[str, Any]:
        config = _resolve_config(node.get("config", {}), ctx.variables)
        selector = str(config.get("selector", "")).strip()
        timeout_ms = int(config.get("timeout_ms", 10000) or 10000)
        wait_ms = int(config.get("wait_ms", 0) or 0)

        if selector:
            await ctx.page.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)
            return {"waited_for_selector": selector, "timeout_ms": timeout_ms}

        if wait_ms > 0:
            await ctx.page.wait_for_timeout(wait_ms)
            return {"waited_ms": wait_ms}

        return {"waited_ms": 0}


class ClickNodeExecutor(NodeExecutor):
    node_type = "click"

    async def execute(self, node: dict[str, Any], ctx: RuntimeContext) -> dict[str, Any]:
        config = _resolve_config(node.get("config", {}), ctx.variables)
        selector = str(config.get("selector", "")).strip()
        text = str(config.get("text", "")).strip()

        if selector:
            await ctx.page.locator(selector).first.click(timeout=5000)
            return {"clicked_selector": selector}

        if text:
            button = ctx.page.get_by_role("button", name=text)
            if await button.count() > 0:
                await button.first.click(timeout=5000)
                return {"clicked_text": text, "role": "button"}

            link = ctx.page.get_by_role("link", name=text)
            if await link.count() > 0:
                await link.first.click(timeout=5000)
                return {"clicked_text": text, "role": "link"}

            target = ctx.page.get_by_text(text).first
            if await target.count() > 0:
                await target.click(timeout=5000)
                return {"clicked_text": text, "role": "text"}

        raise NodeExecutionError("Nodo click requiere selector o text valido")


class ScreenshotNodeExecutor(NodeExecutor):
    node_type = "screenshot"

    async def execute(self, node: dict[str, Any], ctx: RuntimeContext) -> dict[str, Any]:
        if not ctx.artifacts_dir:
            raise NodeExecutionError("artifacts_dir no disponible para screenshot")

        config = _resolve_config(node.get("config", {}), ctx.variables)
        file_name = str(config.get("file_name", "")).strip() or f"{ctx.screenshot_name}_node.png"
        safe_name = _safe_file_name(file_name)
        file_path = str(ctx.artifacts_dir / safe_name)
        await ctx.page.screenshot(path=file_path, full_page=True)
        ctx.evidence_screenshots.append(file_path)
        return {"screenshot": file_path}


class VideoNodeExecutor(NodeExecutor):
    node_type = "video"

    async def execute(self, node: dict[str, Any], ctx: RuntimeContext) -> dict[str, Any]:
        # Nodo marcador para activar captura de video en el runner.
        ctx.log("[WF] Nodo video detectado: la grabación se controla a nivel de contexto Playwright")
        return {
            "video_enabled": True,
            "node_id": str(node.get("id", "")),
        }


def build_default_node_factory() -> NodeFactory:
    factory = NodeFactory()
    factory.register("login", LoginNodeExecutor)
    factory.register("navigate", NavigateNodeExecutor)
    factory.register("form", FillFormNodeExecutor)
    factory.register("validation", ValidationNodeExecutor)
    factory.register("upload", UploadNodeExecutor)
    factory.register("wait", WaitNodeExecutor)
    factory.register("click", ClickNodeExecutor)
    factory.register("screenshot", ScreenshotNodeExecutor)
    factory.register("video", VideoNodeExecutor)
    return factory


def _safe_file_name(value: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
    normalized = "".join(ch if ch in allowed else "_" for ch in value).strip("_")
    return normalized or "screenshot.png"


def _resolve_string_template(value: str, variables: dict[str, Any]) -> str:
    pattern = re.compile(r"\{\{\s*([a-zA-Z0-9_\-.]+)\s*\}\}")

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(variables.get(key, ""))

    return pattern.sub(repl, value)


def _resolve_config(config: dict[str, Any], variables: dict[str, Any]) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for key, value in config.items():
        if isinstance(value, str):
            resolved[key] = _resolve_string_template(value, variables)
        else:
            resolved[key] = value
    return resolved


def _extract_form_fields(config: dict[str, Any]) -> list[dict[str, Any]]:
    raw_fields = config.get("fields")
    if isinstance(raw_fields, list):
        return [field for field in raw_fields if isinstance(field, dict)]

    raw_map = config.get("field_map")
    if isinstance(raw_map, dict):
        return [{"selector": str(selector), "value": str(value)} for selector, value in raw_map.items()]

    raw_json = config.get("field_map_json")
    if isinstance(raw_json, str) and raw_json.strip():
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                return [{"selector": str(selector), "value": str(value)} for selector, value in parsed.items()]
            if isinstance(parsed, list):
                return [field for field in parsed if isinstance(field, dict)]
        except Exception:  # noqa: BLE001
            return []

    return []
