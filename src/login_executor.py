"""Servicio de login automático para Playwright con soporte 2Captcha"""
import asyncio
import logging
import re
import time

from playwright.async_api import Page

from captcha_service import TwoCaptchaService
from config import settings

logger = logging.getLogger(__name__)


class LoginExecutor:
    """Ejecuta login automático en aplicaciones web"""

    _SESSION_EXPIRED_MARKERS = (
        "tu sesión ha expirado",
        "tu sesion ha expirado",
        "expiró tu token",
        "expiro tu token",
        "se ha iniciado sesión en otro equipo",
        "se ha iniciado sesion en otro equipo",
    )

    def __init__(self):
        self.captcha_service = TwoCaptchaService(
            api_key=settings.two_captcha_api_key,
            timeout=settings.two_captcha_timeout,
        )

    async def execute_login(
        self,
        page: Page,
        url: str,
        username: str,
        password: str,
        has_captcha: bool = False,
        playwright_plan: dict | None = None,
    ) -> dict:
        """
        Ejecuta login automático.
        
        Args:
            page: Instancia de Playwright Page
            url: URL de la aplicación
            username: Usuario/email
            password: Contraseña
            has_captcha: Si la página tiene CAPTCHA
            
        Returns:
            dict con resultado: {"success": bool, "message": str}
        """
        logs = []
        try:
            if playwright_plan and playwright_plan.get("actions"):
                return await self._execute_plan(
                    page=page,
                    url=url,
                    username=username,
                    password=password,
                    has_captcha=has_captcha,
                    plan=playwright_plan,
                    logs=logs,
                )

            # 1. Navegar a la URL (fallback clásico)
            logs.append(f"Navegando a {url}")
            await page.goto(url, wait_until="domcontentloaded")
            logs.append("Página cargada")
            await self._close_session_expired_alert_if_present(page, logs)

            # 2. Llenar usuario
            logs.append(f"Ingresando usuario: {username}")
            user_locators = self._build_user_locators(page)
            filled = await self._fill_first_available(user_locators, username, logs, "usuario")
            if not filled and await self._close_session_expired_alert_if_present(page, logs):
                logs.append("Reintentando ingreso de usuario tras cerrar alerta")
                filled = await self._fill_first_available(user_locators, username, logs, "usuario")
            if not filled:
                raise Exception("No se encontró campo de usuario")

            # 3. Llenar contraseña
            logs.append(f"Ingresando contraseña")
            password_locators = self._build_password_locators(page)
            filled = await self._fill_first_available(password_locators, password, logs, "contraseña")
            if not filled and await self._close_session_expired_alert_if_present(page, logs):
                logs.append("Reintentando ingreso de contraseña tras cerrar alerta")
                filled = await self._fill_first_available(password_locators, password, logs, "contraseña")
            if not filled:
                raise Exception("No se encontró campo de contraseña")

            # 4. Si hay CAPTCHA, resolverlo
            if has_captcha or self.captcha_service.is_enabled():
                logs.append("Detectado CAPTCHA, intentando resolver...")
                captcha_resolved = await self._resolve_captcha(page, logs)
                if not captcha_resolved:
                    return {"success": False, "message": "No se pudo resolver CAPTCHA", "logs": logs}

            # 5. Hacer click en botón de login
            logs.append("Buscando botón de envío...")
            submit_selectors = [
                "button[type='submit']",
                "button:has-text('Login')",
                "button:has-text('Ingresar')",
                "button:has-text('Entrar')",
                "input[type='submit']",
            ]

            clicked = False
            for selector in submit_selectors:
                try:
                    await page.click(selector)
                    clicked = True
                    logs.append(f"Botón encontrado y clickeado: {selector}")
                    break
                except Exception:
                    continue

            if not clicked:
                clicked = await self._click_by_role_or_text(page, ["login", "ingresar", "entrar", "iniciar sesión", "acceder"], logs)

            if not clicked:
                raise Exception("No se encontró botón de envío")

            # Si había CAPTCHA, esperar a que el botón procese (puede quedar deshabilitado brevemente)
            if has_captcha or self.captcha_service.is_enabled():
                await self._wait_for_submit_enabled(page, logs)

            # 6. Esperar a que se complete el login (cambio de URL o elemento visible)
            logs.append("Esperando resultado del login...")
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
                await self._wait_for_post_login_transition(page, logs, baseline_url=url, timeout_ms=15000)
                logs.append("Login completado")
                return {"success": True, "message": "Login exitoso", "logs": logs}
            except Exception:
                # Si no hay networkidle, asumir que fue exitoso si no hay error
                logs.append("Proceso de login completado (timeout en networkidle)")
                return {"success": True, "message": "Login completado", "logs": logs}

        except Exception as exc:
            logs.append(f"Error durante login: {exc}")
            logger.error(f"Login error: {exc}", exc_info=True)
            return {"success": False, "message": str(exc), "logs": logs}

    async def _execute_plan(
        self,
        page: Page,
        url: str,
        username: str,
        password: str,
        has_captcha: bool,
        plan: dict,
        logs: list,
    ) -> dict:
        selectors = plan.get("selectors", {}) if isinstance(plan, dict) else {}
        actions = plan.get("actions", []) if isinstance(plan, dict) else []

        variables = {
            "username": username,
            "password": password,
            "url": url,
        }

        for action in actions:
            await self._close_session_expired_alert_if_present(page, logs)
            action_type = action.get("type")

            if action_type == "goto":
                target_url = str(action.get("url") or url)
                logs.append(f"[PLAN] goto {target_url}")
                await page.goto(target_url, wait_until="domcontentloaded")
                continue

            if action_type == "fill":
                field = str(action.get("field", ""))
                raw_value = str(action.get("value", ""))
                value = self._resolve_variable(raw_value, variables)
                selector_list = self._merge_action_selectors(action, selectors.get(field, []))
                ok = await self._fill_with_selectors(page, selector_list, value, logs, field)
                if not ok:
                    ok = await self._fill_with_locator_strategy(page, action, value, logs, field)
                if not ok:
                    ok = await self._fill_by_field_fallback(page, field, value, logs)
                if not ok and await self._close_session_expired_alert_if_present(page, logs):
                    logs.append(f"[PLAN] Reintentando fill '{field}' tras cerrar alerta")
                    ok = await self._fill_with_selectors(page, selector_list, value, logs, field)
                    if not ok:
                        ok = await self._fill_with_locator_strategy(page, action, value, logs, field)
                    if not ok:
                        ok = await self._fill_by_field_fallback(page, field, value, logs)
                if not ok:
                    raise Exception(f"No se encontró campo '{field}' para fill")
                continue

            if action_type == "click":
                field = str(action.get("field", "submit"))
                selector_list = self._merge_action_selectors(action, selectors.get(field, []))
                ok = await self._click_with_selectors(page, selector_list, logs, field)
                if not ok:
                    ok = await self._click_with_locator_strategy(page, action, logs, field)
                if not ok:
                    labels = self._build_click_labels(action, field)
                    ok = await self._click_by_role_or_text(page, labels, logs)
                if not ok:
                    ok = await self._click_generic_submit(page, logs)
                if not ok and await self._close_session_expired_alert_if_present(page, logs):
                    logs.append(f"[PLAN] Reintentando click '{field}' tras cerrar alerta")
                    ok = await self._click_with_selectors(page, selector_list, logs, field)
                    if not ok:
                        ok = await self._click_with_locator_strategy(page, action, logs, field)
                    if not ok:
                        labels = self._build_click_labels(action, field)
                        ok = await self._click_by_role_or_text(page, labels, logs)
                    if not ok:
                        ok = await self._click_generic_submit(page, logs)
                if not ok:
                    raise Exception(f"No se encontró botón/click target '{field}'")
                continue

            if action_type == "wait_for_selector":
                selector_list = self._merge_action_selectors(action, [])
                timeout_ms = int(action.get("timeout_ms", 10000))
                ok = await self._wait_for_selector(page, selector_list, timeout_ms, logs)
                if not ok and await self._close_session_expired_alert_if_present(page, logs):
                    logs.append("[PLAN] Reintentando wait_for_selector tras cerrar alerta")
                    ok = await self._wait_for_selector(page, selector_list, timeout_ms, logs)
                if not ok:
                    raise Exception("No se encontró selector esperado en wait_for_selector")
                continue

            if action_type == "wait_network_idle":
                timeout_ms = int(action.get("timeout_ms", 10000))
                logs.append(f"[PLAN] wait_network_idle {timeout_ms}ms")
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)
                continue

            if action_type == "solve_captcha":
                if has_captcha or self.captcha_service.is_enabled():
                    logs.append("[PLAN] Resolviendo CAPTCHA antes del click...")
                    captcha_ok = await self._resolve_captcha(page, logs)
                    if not captcha_ok:
                        return {"success": False, "message": "No se pudo resolver CAPTCHA", "logs": logs}
                    # Esperar a que el botón de submit esté habilitado tras CAPTCHA
                    await self._wait_for_submit_enabled(page, logs)
                else:
                    logs.append("[PLAN] solve_captcha: 2Captcha no configurado, se omite")
                continue

        await self._wait_for_post_login_transition(page, logs, baseline_url=url, timeout_ms=15000)
        return {"success": True, "message": "Login ejecutado con plan IA", "logs": logs}

    async def _fill_with_selectors(self, page: Page, selectors: list, value: str, logs: list, field: str) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(str(selector)).first
                await locator.fill(value)
                logs.append(f"[PLAN] fill {field} con selector: {selector}")
                return True
            except Exception:
                continue
        return False

    async def _fill_with_locator_strategy(self, page: Page, action: dict, value: str, logs: list, field: str) -> bool:
        text_value = str(action.get("text") or action.get("name") or "").strip()
        test_id = str(action.get("test_id") or "").strip()

        candidates = []
        if test_id:
            candidates.append(page.get_by_test_id(test_id))
        if text_value:
            candidates.append(page.get_by_label(re.compile(text_value, re.I)))
            candidates.append(page.get_by_placeholder(re.compile(text_value, re.I)))
            candidates.append(page.get_by_role("textbox", name=re.compile(text_value, re.I)))

        for locator in candidates:
            try:
                if await locator.count() <= 0:
                    continue
                await locator.first.fill(value)
                logs.append(f"[PLAN] fill {field} con locator semántico")
                return True
            except Exception:
                continue
        return False

    async def _fill_by_field_fallback(self, page: Page, field: str, value: str, logs: list) -> bool:
        if field.lower() in {"username", "user", "email", "usuario", "login"}:
            return await self._fill_first_available(self._build_user_locators(page), value, logs, "usuario")
        if field.lower() in {"password", "pass", "clave", "contraseña"}:
            return await self._fill_first_available(self._build_password_locators(page), value, logs, "contraseña")
        return False

    def _build_user_locators(self, page: Page) -> list:
        return [
            page.get_by_label(re.compile(r"usuario|user|email|correo|login", re.I)),
            page.get_by_placeholder(re.compile(r"usuario|user|email|correo|login", re.I)),
            page.get_by_role("textbox", name=re.compile(r"usuario|user|email|correo|login", re.I)),
            page.locator("input[type='email']"),
            page.locator("input[name='email']"),
            page.locator("input[name='username']"),
            page.locator("input[name='user']"),
            page.locator("input[autocomplete='username']"),
            page.locator("input:not([type='hidden']):not([type='password'])").first,
        ]

    def _build_password_locators(self, page: Page) -> list:
        return [
            page.get_by_label(re.compile(r"contraseñ|password|clave|pass", re.I)),
            page.get_by_placeholder(re.compile(r"contraseñ|password|clave|pass", re.I)),
            page.get_by_role("textbox", name=re.compile(r"contraseñ|password|clave|pass", re.I)),
            page.locator("input[type='password']"),
            page.locator("input[name='password']"),
            page.locator("input[autocomplete='current-password']"),
        ]

    async def _fill_first_available(self, locators: list, value: str, logs: list, field: str) -> bool:
        for locator in locators:
            try:
                count = await locator.count()
                if count <= 0:
                    continue
                target = locator.first if count > 1 else locator
                await target.fill(value)
                logs.append(f"{field.capitalize()} ingresado con locator: {getattr(target, '_selector', 'accessible')}")
                return True
            except Exception:
                continue
        return False

    def _normalize_selector_list(self, selectors: object) -> list:
        if isinstance(selectors, list):
            return [str(selector) for selector in selectors if str(selector).strip()]
        if isinstance(selectors, str) and selectors.strip():
            return [selectors.strip()]
        return []

    async def _click_by_role_or_text(self, page: Page, labels: list[str], logs: list) -> bool:
        for label in labels:
            try:
                button = page.get_by_role("button", name=re.compile(label, re.I))
                if await button.count() > 0:
                    await button.first.click()
                    logs.append(f"Botón clickeado por rol/texto: {label}")
                    return True
            except Exception:
                continue

            try:
                link = page.get_by_role("link", name=re.compile(label, re.I))
                if await link.count() > 0:
                    await link.first.click()
                    logs.append(f"Elemento clickeado por rol link/texto: {label}")
                    return True
            except Exception:
                continue

            try:
                text_target = page.get_by_text(re.compile(label, re.I))
                if await text_target.count() > 0:
                    await text_target.first.click()
                    logs.append(f"Elemento clickeado por texto: {label}")
                    return True
            except Exception:
                continue
        return False

    async def _click_with_selectors(self, page: Page, selectors: list, logs: list, field: str) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(str(selector)).first
                await locator.click()
                logs.append(f"[PLAN] click {field} con selector: {selector}")
                return True
            except Exception:
                continue
        return False

    async def _click_with_locator_strategy(self, page: Page, action: dict, logs: list, field: str) -> bool:
        role = str(action.get("role") or "").strip()
        name = str(action.get("name") or action.get("text") or "").strip()
        test_id = str(action.get("test_id") or "").strip()

        candidates = []
        if test_id:
            candidates.append(page.get_by_test_id(test_id))
        if role and name:
            try:
                candidates.append(page.get_by_role(role, name=re.compile(name, re.I)))
            except Exception:
                pass
        if name:
            candidates.append(page.get_by_text(re.compile(name, re.I)))

        for locator in candidates:
            try:
                if await locator.count() <= 0:
                    continue
                await locator.first.click()
                logs.append(f"[PLAN] click {field} con locator semántico")
                return True
            except Exception:
                continue
        return False

    async def _wait_for_post_login_transition(
        self,
        page: Page,
        logs: list,
        baseline_url: str | None = None,
        timeout_ms: int = 15000,
    ) -> bool:
        deadline = time.monotonic() + max(0, timeout_ms) / 1000

        while time.monotonic() < deadline:
            try:
                current_url = page.url
                url_changed = bool(baseline_url and current_url and current_url != baseline_url)
                processing_visible = await self._is_visible(page, [
                    page.get_by_text(re.compile(r"procesando,?\s*por favor espere", re.I)),
                    page.get_by_text(re.compile(r"procesando", re.I)),
                ])
                login_surface_visible = await self._is_visible(page, [
                    page.locator("input[type='password']"),
                    page.get_by_role("button", name=re.compile(r"iniciar sesi[oó]n|login|ingresar|entrar|acceder", re.I)),
                    page.get_by_text(re.compile(r"iniciar sesi[oó]n|login|ingresar|entrar|acceder", re.I)),
                ])

                if url_changed or (not processing_visible and not login_surface_visible):
                    if url_changed:
                        logs.append(f"[LOGIN] Transición post-login detectada: {baseline_url} -> {current_url}")
                    else:
                        logs.append("[LOGIN] La superficie de login ya no está visible")
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    return True
            except Exception:
                pass

            await page.wait_for_timeout(500)

        logs.append("[LOGIN] No se confirmó la transición post-login dentro del tiempo esperado; continúo con la navegación")
        return False

    async def _is_visible(self, page: Page, locators: list) -> bool:
        for locator in locators:
            try:
                if await locator.count() > 0 and await locator.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _wait_for_selector(self, page: Page, selectors: list, timeout_ms: int, logs: list) -> bool:
        for selector in selectors:
            try:
                await page.locator(str(selector)).first.wait_for(state="visible", timeout=timeout_ms)
                logs.append(f"[PLAN] wait_for_selector con selector: {selector}")
                return True
            except Exception:
                continue
        return False

    def _merge_action_selectors(self, action: dict, fallback_selectors: object) -> list:
        merged = self._normalize_selector_list(fallback_selectors)

        action_selectors = action.get("selectors")
        merged = self._append_unique(merged, self._normalize_selector_list(action_selectors))

        for key in ["selector", "css", "xpath"]:
            value = action.get(key)
            if isinstance(value, str) and value.strip():
                merged = self._append_unique(merged, [value.strip()])

        return merged

    # Nombres de campo que son tags HTML genéricos — no sirven como texto de búsqueda
    _GENERIC_HTML_TAGS = {"button", "input", "a", "div", "span", "form", "submit"}

    def _build_click_labels(self, action: dict, field: str) -> list[str]:
        # Excluir field si es un tag HTML genérico para no buscar texto "button" literalmente
        field_label = "" if field.lower() in self._GENERIC_HTML_TAGS else field
        labels = [
            str(action.get("text") or "").strip(),
            str(action.get("name") or "").strip(),
            field_label,
            "login",
            "ingresar",
            "entrar",
            "iniciar sesión",
            "acceder",
            "continuar",
            "sign in",
            "siguiente",
            "next",
        ]
        return [label for label in labels if label]

    async def _click_generic_submit(self, page: Page, logs: list) -> bool:
        """Último recurso: intenta clickear el botón de envío más probable en el DOM."""
        css_candidates = [
            "button[type='submit']",
            "input[type='submit']",
            "button:not([type='button']):not([type='reset'])",
            "[role='button']",
            "button",
        ]
        for selector in css_candidates:
            try:
                locator = page.locator(selector).last
                if await locator.count() > 0:
                    await locator.click()
                    logs.append(f"[FALLBACK] click genérico con selector: {selector}")
                    return True
            except Exception:
                continue
        return False

    def _append_unique(self, base: list[str], values: list[str]) -> list[str]:
        result = list(base)
        for value in values:
            if value not in result:
                result.append(value)
        return result

    def _resolve_variable(self, raw_value: str, variables: dict) -> str:
        if raw_value.startswith("{{") and raw_value.endswith("}}"):
            key = raw_value[2:-2].strip()
            return str(variables.get(key, ""))
        return raw_value

    async def _resolve_captcha(self, page: Page, logs: list) -> bool:
        """Intenta resolver CAPTCHA automáticamente"""
        return await self._resolve_captcha_impl(page, logs, attempt=1, max_attempts=2)

    async def _resolve_captcha_impl(
        self, page: Page, logs: list, attempt: int = 1, max_attempts: int = 2
    ) -> bool:
        """Implementación de resolución con reintentos automáticos."""
        try:
            if not self.captcha_service.is_enabled():
                logs.append("2Captcha no está configurado; se omite resolución automática")
                return True

            sitekey = await self._detect_sitekey(page)
            has_recaptcha = await page.locator("iframe[src*='recaptcha']").count() > 0
            has_hcaptcha = await page.locator("iframe[src*='hcaptcha']").count() > 0

            if not sitekey and not has_recaptcha and not has_hcaptcha:
                logs.append("No se detectó CAPTCHA visible para resolver")
                return True

            if not sitekey:
                logs.append("Se detectó iframe de CAPTCHA, pero no se encontró sitekey")
                return False

            current_url = page.url
            user_agent, cookie_header = await self._build_captcha_session_context(page, current_url)
            recaptcha_data_s = await self._detect_recaptcha_data_s(page)
            if recaptcha_data_s:
                logs.append("ReCAPTCHA data-s detectado para sesión actual")

            if has_hcaptcha and not has_recaptcha:
                logs.append(f"[Intento {attempt}/{max_attempts}] hCaptcha detectado, sitekey: {sitekey[:20]}...")
                result = await asyncio.to_thread(
                    self.captcha_service.solve_hcaptcha,
                    sitekey,
                    current_url,
                    logs.append,
                    user_agent,
                    cookie_header,
                )
                if not (result.success and result.code):
                    logs.append(f"Error resolviendo hCaptcha: {result.error}")
                    return False
                await self._inject_hcaptcha_token(page, result.code)
                logs.append("hCaptcha resuelto e inyectado por 2Captcha")

            else:
                logs.append(f"[Intento {attempt}/{max_attempts}] ReCAPTCHA detectado, sitekey: {sitekey[:20]}...")
                # Esperar a que el usuario resuelva el CAPTCHA manualmente
                captcha_resolved = await self._wait_for_manual_captcha_resolution(page, logs)
                if not captcha_resolved:
                    return False

            # Esperar brevemente y detectar si el backend rechazó el token
            await asyncio.sleep(2)
            await self._close_session_expired_alert_if_present(page, logs)

            # Verificar si el iframe de CAPTCHA reaparece (indicador de rechazo)
            iframe_count = await page.locator("iframe[src*='recaptcha'], iframe[src*='hcaptcha']").count()
            if iframe_count > 0 and attempt < max_attempts:
                # NO recargar la página: mantener los campos llenos y esperar al usuario
                logs.append("[FALLBACK] CAPTCHA rechazado o aún activo — esperando nueva resolución manual sin recargar página...")
                captcha_ok = await self._wait_for_manual_captcha_resolution(page, logs)
                if not captcha_ok:
                    return False
                await asyncio.sleep(1)

            return True

        except Exception as exc:
            logs.append(f"Error resolviendo CAPTCHA: {exc}")
            logger.error(f"CAPTCHA resolution error: {exc}", exc_info=True)
            return False

    async def _detect_sitekey(self, page: Page) -> str | None:
        sitekey_locator = page.locator("[data-sitekey]").first
        if await sitekey_locator.count() > 0:
            sitekey = await sitekey_locator.get_attribute("data-sitekey")
            if sitekey:
                return sitekey

        recaptcha_frame = page.locator("iframe[src*='recaptcha'][src*='k=']").first
        if await recaptcha_frame.count() > 0:
            src = await recaptcha_frame.get_attribute("src")
            match = re.search(r"[?&]k=([^&]+)", src or "")
            if match:
                return match.group(1)

        return None

    async def _detect_recaptcha_data_s(self, page: Page) -> str | None:
        """Extrae parámetro data-s/s que algunos backends validan junto al token."""
        locator = page.locator("[data-s]").first
        if await locator.count() > 0:
            value = await locator.get_attribute("data-s")
            if value:
                return value

        recaptcha_frame = page.locator("iframe[src*='recaptcha'][src*='s=']").first
        if await recaptcha_frame.count() > 0:
            src = await recaptcha_frame.get_attribute("src")
            match = re.search(r"[?&]s=([^&]+)", src or "")
            if match:
                return match.group(1)

        return None

    async def _build_captcha_session_context(self, page: Page, current_url: str) -> tuple[str | None, str | None]:
        """Arma contexto de la sesión actual para que 2Captcha use el mismo fingerprint."""
        user_agent: str | None = None
        try:
            user_agent = await page.evaluate("() => navigator.userAgent")
        except Exception:
            user_agent = None

        cookie_header: str | None = None
        try:
            cookies = await page.context.cookies([current_url])
            parts: list[str] = []
            for item in cookies:
                name = str(item.get("name") or "").strip()
                value = str(item.get("value") or "").strip()
                if name:
                    parts.append(f"{name}={value}")
            if parts:
                cookie_header = "; ".join(parts)
        except Exception:
            cookie_header = None

        return user_agent, cookie_header

    async def _inject_recaptcha_token(self, page: Page, token: str) -> None:
        await page.evaluate(
            """
            (captchaToken) => {
                const selectors = [
                    "textarea#g-recaptcha-response",
                    "textarea[name='g-recaptcha-response']",
                    "#g-recaptcha-response",
                ];

                const nodes = selectors
                    .map((selector) => Array.from(document.querySelectorAll(selector)))
                    .flat();

                nodes.forEach((node) => {
                    node.value = captchaToken;
                    node.innerHTML = captchaToken;
                    node.dispatchEvent(new Event("input", { bubbles: true }));
                    node.dispatchEvent(new Event("change", { bubbles: true }));
                });

                const forms = Array.from(document.querySelectorAll("form"));
                forms.forEach((form) => {
                    if (!form.querySelector("textarea[name='g-recaptcha-response']")) {
                        return;
                    }
                    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
                });

                const cfg = window.___grecaptcha_cfg;
                if (!cfg || !cfg.clients) {
                    return;
                }

                const callCallbacks = (obj) => {
                    if (!obj || typeof obj !== "object") {
                        return;
                    }
                    Object.values(obj).forEach((value) => {
                        if (typeof value === "function") {
                            try {
                                value(captchaToken);
                            } catch (_error) {
                                // Ignorar callbacks incompatibles
                            }
                            return;
                        }
                        callCallbacks(value);
                    });
                };

                callCallbacks(cfg.clients);
            }
            """,
            token,
        )

    async def _inject_hcaptcha_token(self, page: Page, token: str) -> None:
        await page.evaluate(
            """
            (captchaToken) => {
                const selectors = [
                    "textarea[name='h-captcha-response']",
                    "textarea[name='g-recaptcha-response']",
                    "#h-captcha-response",
                ];

                const nodes = selectors
                    .map((selector) => Array.from(document.querySelectorAll(selector)))
                    .flat();

                nodes.forEach((node) => {
                    node.value = captchaToken;
                    node.innerHTML = captchaToken;
                    node.dispatchEvent(new Event("input", { bubbles: true }));
                    node.dispatchEvent(new Event("change", { bubbles: true }));
                });

                if (window.hcaptcha && typeof window.hcaptcha.execute === "function") {
                    try {
                        window.hcaptcha.execute();
                    } catch (_error) {
                        // Ignorar si no aplica a este widget
                    }
                }
            }
            """,
            token,
        )

    async def _wait_for_submit_enabled(self, page: Page, logs: list, timeout_s: float = 10.0) -> None:
        """Espera hasta que el botón de submit no esté deshabilitado (máx timeout_s seg)."""
        submit_selectors = [
            "button[type='submit']:not([disabled])",
            "input[type='submit']:not([disabled])",
            "button:not([disabled]):has-text('Login')",
            "button:not([disabled]):has-text('Ingresar')",
            "button:not([disabled]):has-text('Entrar')",
            "button:not([disabled]):has-text('Iniciar')",
            "button:not([disabled]):has-text('Acceder')",
        ]
        start = time.time()
        while time.time() - start < timeout_s:
            for sel in submit_selectors:
                try:
                    count = await page.locator(sel).count()
                    if count > 0:
                        logs.append(f"[CAPTCHA] Botón de login habilitado, procediendo...")
                        return
                except Exception:
                    pass
            await asyncio.sleep(0.5)
        logs.append("[CAPTCHA] Timeout esperando botón habilitado, intentando de todas formas")

    async def _wait_for_manual_captcha_resolution(self, page: Page, logs: list, timeout_ms: int = 300000) -> bool:
        """Espera a que el usuario resuelva manualmente el CAPTCHA.
        Monitorea el textarea g-recaptcha-response para detectar cuando tiene un token."""
        try:
            logs.append("[MANUAL] Esperando resolución manual del CAPTCHA por el usuario...")
            
            start_time = time.time()
            timeout_s = timeout_ms / 1000
            
            while time.time() - start_time < timeout_s:
                # Verificar si el textarea del CAPTCHA tiene valor
                token_value = await page.evaluate(
                    """
                    () => {
                        const selectors = [
                            "textarea#g-recaptcha-response",
                            "textarea[name='g-recaptcha-response']",
                            "#g-recaptcha-response",
                        ];
                        const nodes = selectors
                            .map((selector) => Array.from(document.querySelectorAll(selector)))
                            .flat();
                        for (const node of nodes) {
                            if (node.value && node.value.trim().length > 10) {
                                return node.value;
                            }
                        }
                        return null;
                    }
                    """
                )
                
                if token_value:
                    elapsed = int(time.time() - start_time)
                    logs.append(f"[MANUAL] CAPTCHA resuelto por usuario en {elapsed}s")
                    return True
                
                # Mostrar progreso cada 30 segundos
                elapsed = int(time.time() - start_time)
                if elapsed % 30 == 0 and elapsed > 0:
                    remaining = int(timeout_s - elapsed)
                    logs.append(f"[MANUAL] Esperando... {elapsed}s transcurridos ({remaining}s restantes)")
                
                await asyncio.sleep(2)
            
            logs.append(f"[MANUAL] Timeout esperando CAPTCHA manual (>{timeout_s}s)")
            return False
            
        except Exception as exc:
            logs.append(f"[MANUAL] Error esperando resolución manual: {exc}")
            return False

    async def _close_session_expired_alert_if_present(self, page: Page, logs: list) -> bool:
        """Cierra popup swal2 de sesión expirada y recaptura estado del DOM."""
        try:
            popup = page.locator(".swal2-popup.swal2-modal.swal2-show").first
            if await popup.count() <= 0:
                return False

            popup_text = (await popup.inner_text() or "").lower()
            if not any(marker in popup_text for marker in self._SESSION_EXPIRED_MARKERS):
                return False

            logs.append("[ALERT] Detectada alerta de sesión expirada, cerrando modal...")

            close_candidates = [
                ".swal2-actions .swal2-confirm",
                "button.confirmButtonSweet",
                "button.swal2-confirm",
                ".swal2-close",
            ]
            closed = False
            for selector in close_candidates:
                try:
                    btn = page.locator(selector).first
                    if await btn.count() > 0:
                        await btn.click(timeout=3000)
                        closed = True
                        logs.append(f"[ALERT] Modal cerrado con selector: {selector}")
                        break
                except Exception:
                    continue

            if not closed:
                logs.append("[ALERT] No se pudo clickear botón de cierre del modal")
                return False

            try:
                await popup.wait_for(state="hidden", timeout=3000)
            except Exception:
                pass

            await self._recapture_page_info(page, logs, reason="session_expired_alert_closed")
            return True
        except Exception as exc:
            logs.append(f"[ALERT] Error manejando alerta de sesión expirada: {exc}")
            return False

    async def _recapture_page_info(self, page: Page, logs: list, reason: str) -> None:
        """Captura información actual del DOM para reintentos tras eventos inesperados."""
        try:
            snapshot = await page.evaluate(
                """
                () => ({
                    url: window.location.href,
                    title: document.title || "",
                    inputs: document.querySelectorAll("input").length,
                    buttons: document.querySelectorAll("button").length,
                    forms: document.querySelectorAll("form").length,
                    swalVisible: !!document.querySelector(".swal2-popup.swal2-show")
                })
                """
            )
            logs.append(
                "[RECAPTURE] "
                f"reason={reason}, url={snapshot.get('url')}, "
                f"title='{snapshot.get('title')}', "
                f"forms={snapshot.get('forms')}, inputs={snapshot.get('inputs')}, "
                f"buttons={snapshot.get('buttons')}, swalVisible={snapshot.get('swalVisible')}"
            )
        except Exception as exc:
            logs.append(f"[RECAPTURE] No fue posible capturar estado de página: {exc}")
