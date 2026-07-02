import base64
import json

import httpx

from config import settings


class ApiClient:
    def __init__(self) -> None:
        self.base_url = settings.api_base_url
        self.token: str | None = None
        self.user_id: int | None = None

    async def login(self, email: str, password: str) -> bool:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.base_url}/auth/login",
                json={"email": email, "password": password},
            )
        if response.status_code != 200:
            return False
        self.token = response.json().get("access_token")
        self.user_id = self._extract_user_id_from_token(self.token)
        return True

    async def list_projects(self) -> list[dict]:
        data = await self._get_any("/projects")
        return data if isinstance(data, list) else []

    async def list_tests(self) -> list[dict]:
        data = await self._get_any("/test-cases")
        return data if isinstance(data, list) else []

    async def list_qa_metrics(self) -> list[dict]:
        data = await self._get_any("/metrics/qa-metrics")
        return data if isinstance(data, list) else []

    async def get_login_configs(self, project_id: int) -> list[dict]:
        """Obtiene configuraciones de login para un proyecto"""
        data = await self._get_any(f"/login-configs/project/{project_id}")
        return data if isinstance(data, list) else []

    async def get_login_config(self, config_id: int) -> dict | None:
        """Obtiene una configuración de login específica"""
        config = await self._get_any(f"/login-configs/{config_id}")
        return config if isinstance(config, dict) else None

    async def list_workflows(self, project_id: int) -> list[dict]:
        """Obtiene workflows visuales del proyecto (API incremental)."""
        data = await self._get_any(f"/workflows?project_id={project_id}")
        return data if isinstance(data, list) else []

    async def get_workflow(self, workflow_id: int) -> dict | None:
        """Obtiene una definición de workflow visual por id."""
        data = await self._get_any(f"/workflows/{workflow_id}")
        return data if isinstance(data, dict) else None

    async def analyze_navigation_html(
        self,
        config_id: int,
        section_name: str,
        html_excerpt: str,
        current_url: str = "",
    ) -> dict | None:
        """Envía HTML renderizado para obtener selectores de navegación sugeridos por IA."""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload = {
            "section_name": section_name,
            "html_excerpt": html_excerpt,
            "current_url": current_url,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.base_url}/login-configs/{config_id}/navigation/analyze",
                json=payload,
                headers=headers,
            )
        if response.status_code not in (200, 201):
            return None
        data = response.json()
        return data if isinstance(data, dict) else None

    # ── Execution management ──────────────────────────────────────────────────

    async def create_execution(self, project_id: int, login_config_id: int | None = None) -> dict | None:
        """Crea un registro de ejecución en el backend y devuelve {id, ...}."""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload = {
            "project_id": project_id,
            "login_config_id": login_config_id,
            "test_case_id": None,
            "status": "running",
        }
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.base_url}/executions",
                json=payload,
                headers=headers,
            )
        if response.status_code not in (200, 201):
            return None
        return response.json()

    async def update_execution_status(self, execution_id: int, status: str) -> bool:
        """Actualiza el status de una ejecución (running/passed/failed)."""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.patch(
                f"{self.base_url}/executions/{execution_id}/status",
                json={"status": status},
                headers=headers,
            )
        return response.status_code in (200, 204)

    async def upload_screenshot(
        self,
        execution_id: int,
        file_path: str,
        project_name: str,
        test_name: str,
        iteration: str,
    ) -> str | None:
        """Sube un screenshot al backend y devuelve la URL de almacenamiento."""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        import os

        file_name = os.path.basename(file_path)
        async with httpx.AsyncClient(timeout=60) as client:
            with open(file_path, "rb") as fh:
                response = await client.post(
                    f"{self.base_url}/executions/{execution_id}/screenshots",
                    headers=headers,
                    files={"file": (file_name, fh, "image/png")},
                    data={
                        "user_id": str(self.user_id or 0),
                        "project_name": project_name,
                        "test_name": test_name,
                        "iteration": iteration,
                    },
                )
        if response.status_code not in (200, 201):
            detail = response.text.strip()
            if len(detail) > 200:
                detail = detail[:200] + "..."
            print(f"[desktop-runner] upload_screenshot failed status={response.status_code} detail={detail}")
            return None
        return response.json().get("storage_url")

    async def upload_video(
        self,
        execution_id: int,
        file_path: str,
        project_name: str,
        test_name: str,
        iteration: str,
        duration_seconds: int = 0,
    ) -> str | None:
        """Sube un video al backend y devuelve la URL de almacenamiento."""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        import os

        file_name = os.path.basename(file_path)
        async with httpx.AsyncClient(timeout=120) as client:
            with open(file_path, "rb") as fh:
                response = await client.post(
                    f"{self.base_url}/executions/{execution_id}/video",
                    headers=headers,
                    files={"file": (file_name, fh, "video/webm")},
                    data={
                        "project_name": project_name,
                        "test_name": test_name,
                        "iteration": iteration,
                        "duration_seconds": str(duration_seconds),
                    },
                )
        if response.status_code not in (200, 201):
            detail = response.text.strip()
            if len(detail) > 200:
                detail = detail[:200] + "..."
            print(f"[desktop-runner] upload_video failed status={response.status_code} detail={detail}")
            return None
        return response.json().get("storage_url")

    async def add_execution_log(
        self,
        execution_id: int,
        message: str,
        level: str = "INFO",
        metadata: dict | None = None,
    ) -> bool:
        """Agrega un log de ejecución al backend."""
        import json

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{self.base_url}/executions/{execution_id}/logs",
                headers=headers,
                data={
                    "level": level,
                    "message": message,
                    "metadata_json": json.dumps(metadata or {}),
                },
            )
        return response.status_code in (200, 201)

    async def create_desktop_page_html(
        self,
        *,
        html_content: str,
        page_url: str = "",
        page_title: str = "",
        source: str = "desktop-runner",
        project_id: int | None = None,
        execution_id: int | None = None,
        login_config_id: int | None = None,
        workflow_node_id: str = "",
    ) -> dict | None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload = {
            "project_id": project_id,
            "execution_id": execution_id,
            "login_config_id": login_config_id,
            "workflow_node_id": workflow_node_id,
            "page_url": page_url,
            "page_title": page_title,
            "source": source,
            "html_content": html_content,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/desktop-traces/page-html",
                json=payload,
                headers=headers,
            )
        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"desktop-traces/page-html respondió {response.status_code}: {response.text[:400]}"
            )
        data = response.json()
        return data if isinstance(data, dict) else None

    async def create_desktop_ai_json(
        self,
        *,
        response_json: dict,
        raw_response_text: str = "",
        prompt_text: str = "",
        model_name: str = "",
        page_url: str = "",
        page_title: str = "",
        source: str = "desktop-runner",
        project_id: int | None = None,
        execution_id: int | None = None,
        login_config_id: int | None = None,
        workflow_node_id: str = "",
    ) -> dict | None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        payload = {
            "project_id": project_id,
            "execution_id": execution_id,
            "login_config_id": login_config_id,
            "workflow_node_id": workflow_node_id,
            "page_url": page_url,
            "page_title": page_title,
            "source": source,
            "model_name": model_name,
            "prompt_text": prompt_text,
            "response_json": response_json,
            "raw_response_text": raw_response_text,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/desktop-traces/ai-json",
                json=payload,
                headers=headers,
            )
        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"desktop-traces/ai-json respondió {response.status_code}: {response.text[:400]}"
            )
        data = response.json()
        return data if isinstance(data, dict) else None

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _get(self, path: str) -> list[dict]:
        data = await self._get_any(path)
        return data if isinstance(data, list) else []

    async def _get_any(self, path: str) -> list[dict] | dict | None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"{self.base_url}{path}", headers=headers)
        if response.status_code != 200:
            return None
        return response.json()

    def _extract_user_id_from_token(self, token: str | None) -> int | None:
        if not token:
            return None

        parts = token.split(".")
        if len(parts) < 2:
            return None

        payload_segment = parts[1]
        padding = "=" * (-len(payload_segment) % 4)

        try:
            decoded = base64.urlsafe_b64decode(payload_segment + padding)
            payload = json.loads(decoded.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None

        sub = payload.get("sub")
        try:
            return int(sub)
        except Exception:  # noqa: BLE001
            return None
