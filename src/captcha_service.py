"""Servicio de login automático con soporte 2Captcha para desktop-runner"""
import requests
import time
import logging
from pathlib import Path
from typing import Callable, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CaptchaResult:
    """Resultado de resolución de CAPTCHA"""
    success: bool
    code: Optional[str] = None
    error: Optional[str] = None


class TwoCaptchaService:
    """Cliente para 2Captcha - resuelve ReCAPTCHA, hCaptcha, etc."""

    def __init__(self, api_key: str, timeout: int = 180):
        self.api_key = api_key
        self.timeout = timeout
        self.base_url = "http://2captcha.com"

    def is_enabled(self) -> bool:
        return bool(self.api_key.strip())

    def solve_recaptcha_v2(
        self,
        sitekey: str,
        page_url: str,
        progress_callback: Callable[[str], None] | None = None,
        user_agent: str | None = None,
        cookies: str | None = None,
        recaptcha_data_s: str | None = None,
    ) -> CaptchaResult:
        """Resuelve ReCAPTCHA v2"""
        if not self.api_key:
            return CaptchaResult(success=False, error="2Captcha API key not configured")

        def _log(msg: str) -> None:
            logger.info(msg)
            if progress_callback:
                progress_callback(msg)

        try:
            payload = {
                "method": "userrecaptcha",
                "googlekey": sitekey,
                "pageurl": page_url,
                "key": self.api_key,
            }
            if user_agent:
                payload["userAgent"] = user_agent
            if cookies:
                payload["cookies"] = cookies
            if recaptcha_data_s:
                payload["data-s"] = recaptcha_data_s

            # Enviar CAPTCHA a resolver
            response = requests.post(
                f"{self.base_url}/in.php",
                data=payload,
                timeout=30,
            )
            response.raise_for_status()

            if response.text.startswith("ERROR"):
                return CaptchaResult(success=False, error=response.text)

            captcha_id = response.text.split("|")[1]
            _log(f"[2Captcha] ReCAPTCHA enviado, ID: {captcha_id}. Esperando solución...")

            # Poll para obtener resultado
            start_time = time.time()
            attempt = 0
            while time.time() - start_time < self.timeout:
                time.sleep(5)
                attempt += 1
                elapsed = int(time.time() - start_time)

                poll_response = requests.get(
                    f"{self.base_url}/res.php",
                    params={
                        "key": self.api_key,
                        "id": captcha_id,
                        "action": "get",
                    },
                    timeout=30,
                )
                poll_response.raise_for_status()
                result_text = poll_response.text

                if result_text.startswith("OK|"):
                    code = result_text.split("|")[1]
                    _log(f"[2Captcha] ReCAPTCHA resuelto en {elapsed}s")
                    return CaptchaResult(success=True, code=code)

                if result_text == "CAPCHA_NOT_READY":
                    _log(f"[2Captcha] Procesando... {elapsed}s transcurridos (intento {attempt})")
                    continue

                if result_text.startswith("ERROR"):
                    return CaptchaResult(success=False, error=result_text)

            return CaptchaResult(success=False, error=f"2Captcha timeout ({self.timeout}s)")

        except requests.RequestException as exc:
            return CaptchaResult(success=False, error=f"Request error: {exc}")

    def solve_hcaptcha(
        self,
        sitekey: str,
        page_url: str,
        progress_callback: Callable[[str], None] | None = None,
        user_agent: str | None = None,
        cookies: str | None = None,
    ) -> CaptchaResult:
        """Resuelve hCaptcha"""
        if not self.api_key:
            return CaptchaResult(success=False, error="2Captcha API key not configured")

        def _log(msg: str) -> None:
            logger.info(msg)
            if progress_callback:
                progress_callback(msg)

        try:
            payload = {
                "method": "hcaptcha",
                "sitekey": sitekey,
                "pageurl": page_url,
                "key": self.api_key,
            }
            if user_agent:
                payload["userAgent"] = user_agent
            if cookies:
                payload["cookies"] = cookies

            # Similar a ReCAPTCHA pero con method=hcaptcha
            response = requests.post(
                f"{self.base_url}/in.php",
                data=payload,
                timeout=30,
            )
            response.raise_for_status()

            if response.text.startswith("ERROR"):
                return CaptchaResult(success=False, error=response.text)

            captcha_id = response.text.split("|")[1]
            _log(f"[2Captcha] hCaptcha enviado, ID: {captcha_id}. Esperando solución...")

            # Poll para obtener resultado
            start_time = time.time()
            attempt = 0
            while time.time() - start_time < self.timeout:
                time.sleep(5)
                attempt += 1
                elapsed = int(time.time() - start_time)

                poll_response = requests.get(
                    f"{self.base_url}/res.php",
                    params={
                        "key": self.api_key,
                        "id": captcha_id,
                        "action": "get",
                    },
                    timeout=30,
                )
                poll_response.raise_for_status()
                result_text = poll_response.text

                if result_text.startswith("OK|"):
                    code = result_text.split("|")[1]
                    _log(f"[2Captcha] hCaptcha resuelto en {elapsed}s")
                    return CaptchaResult(success=True, code=code)

                if result_text == "CAPCHA_NOT_READY":
                    _log(f"[2Captcha] Procesando... {elapsed}s transcurridos (intento {attempt})")
                    continue

                if result_text.startswith("ERROR"):
                    return CaptchaResult(success=False, error=result_text)

            return CaptchaResult(success=False, error=f"2Captcha timeout ({self.timeout}s)")

        except requests.RequestException as exc:
            return CaptchaResult(success=False, error=f"Request error: {exc}")
