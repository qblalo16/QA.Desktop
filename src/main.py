import asyncio
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QIcon, QIntValidator
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QPlainTextEdit,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from api_client import ApiClient
from auto_healing import AutoHealingEngine
from config import settings
from playwright_runner import PlaywrightExecutionEngine


def resource_path(relative_path: str) -> str:
    base_path = getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)
    return str(Path(base_path) / relative_path)


def app_icon() -> QIcon:
    icon = QIcon(resource_path('assets/app-icon.ico'))
    if not icon.isNull():
        return icon
    return QIcon(resource_path('assets/app-icon.png'))


class AsyncWorker(QThread):
    message = Signal(str)
    timeline = Signal(str)

    def __init__(
        self,
        test_name: str,
        login_config: dict | None = None,
        api_client: ApiClient | None = None,
        run_count: int = 1,
    ) -> None:
        super().__init__()
        self.test_name = test_name
        self.login_config = login_config
        self.api_client = api_client
        self.run_count = max(1, int(run_count))
        self.runner = PlaywrightExecutionEngine(api_client=api_client)
        self.healing = AutoHealingEngine()

    def run(self) -> None:
        asyncio.run(self._execute())

    async def _execute(self) -> None:
        retries = max(0, self.run_count - 1)
        self.message.emit(f"Ejecuciones solicitadas: {self.run_count} (retries={retries})")
        result = await self.runner.run(self.test_name, retries=retries, login_config=self.login_config)
        for line in result.logs:
            self.message.emit(line)
        for event in result.timeline:
            self.timeline.emit(str(event))
        if result.artifact_urls:
            self.message.emit(f"Artefactos subidos ({len(result.artifact_urls)}): " + ", ".join(result.artifact_urls))
        if result.execution_id:
            self.message.emit(f"Ejecución ID={result.execution_id} finalizada")

        if not result.ok and result.timeline:
            last_error = result.timeline[-1].get("error", "unknown")
            suggestion = await self.healing.suggest_fix(self.test_name, last_error)
            self.message.emit(f"Auto-healing suggestion: {suggestion}")


class MainWindow(QMainWindow):
    VIEW_METRICS = 0
    VIEW_PROJECTS = 1
    VIEW_SETTINGS = 2

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AI QA Desktop Runner")
        self.setWindowIcon(app_icon())
        self.resize(1280, 800)

        self.api = ApiClient()
        self.worker: AsyncWorker | None = None

        self.current_projects: list[dict] = []
        self.current_project_id: int | None = None

        self.auth_stack = QStackedWidget()
        self.login_page = self._build_login_page()
        self.home_page = self._build_home_page()
        self.auth_stack.addWidget(self.login_page)
        self.auth_stack.addWidget(self.home_page)
        self.auth_stack.setCurrentIndex(0)
        self.setCentralWidget(self.auth_stack)

    def _build_login_page(self) -> QWidget:
        page = QWidget()

        container = QVBoxLayout(page)
        container.setAlignment(Qt.AlignmentFlag.AlignCenter)
        container.setSpacing(14)

        card = QWidget()
        card.setFixedWidth(460)
        card_layout = QVBoxLayout(card)
        card_layout.setSpacing(10)

        title = QLabel("Iniciar sesión")
        title.setStyleSheet("font-size: 24px; font-weight: 700;")
        subtitle = QLabel("Accede para ejecutar pruebas automáticas por proyecto")
        subtitle.setStyleSheet("color: #64748b;")

        self.email_input = QLineEdit()
        self.email_input.setPlaceholderText("Email")
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Password")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)

        self.login_btn = QPushButton("Entrar")
        self.login_btn.clicked.connect(self.on_login_clicked)

        self.login_status = QLabel("")
        self.login_status.setStyleSheet("color: #dc2626;")

        card_layout.addWidget(title)
        card_layout.addWidget(subtitle)
        card_layout.addSpacing(4)
        card_layout.addWidget(QLabel("Usuario"))
        card_layout.addWidget(self.email_input)
        card_layout.addWidget(QLabel("Clave"))
        card_layout.addWidget(self.password_input)
        card_layout.addSpacing(8)
        card_layout.addWidget(self.login_btn)
        card_layout.addWidget(self.login_status)

        container.addWidget(card)
        return page

    def _build_home_page(self) -> QWidget:
        page = QWidget()
        root = QHBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = QWidget()
        sidebar.setFixedWidth(240)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(16, 20, 16, 20)
        sidebar_layout.setSpacing(10)

        app_title = QLabel("AI QA Runner")
        app_title.setStyleSheet("font-size: 20px; font-weight: 700;")
        subtitle = QLabel("Centro de ejecución")
        subtitle.setStyleSheet("color: #64748b;")

        self.btn_metrics = QPushButton("Métricas")
        self.btn_projects = QPushButton("Proyectos")
        self.btn_settings = QPushButton("Configuración")
        self.btn_logout = QPushButton("Cerrar sesión")

        self.btn_metrics.clicked.connect(lambda: self.home_stack.setCurrentIndex(self.VIEW_METRICS))
        self.btn_projects.clicked.connect(lambda: self.home_stack.setCurrentIndex(self.VIEW_PROJECTS))
        self.btn_settings.clicked.connect(lambda: self.home_stack.setCurrentIndex(self.VIEW_SETTINGS))
        self.btn_logout.clicked.connect(self.logout)

        sidebar_layout.addWidget(app_title)
        sidebar_layout.addWidget(subtitle)
        sidebar_layout.addSpacing(16)
        sidebar_layout.addWidget(self.btn_metrics)
        sidebar_layout.addWidget(self.btn_projects)
        sidebar_layout.addWidget(self.btn_settings)
        sidebar_layout.addStretch()
        sidebar_layout.addWidget(self.btn_logout)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(20, 20, 20, 20)

        self.home_stack = QStackedWidget()
        self.home_stack.addWidget(self._build_metrics_view())
        self.home_stack.addWidget(self._build_projects_view())
        self.home_stack.addWidget(self._build_settings_view())

        content_layout.addWidget(self.home_stack)

        root.addWidget(sidebar)
        root.addWidget(content)
        return page

    def _build_metrics_view(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)

        title = QLabel("Métricas")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        desc = QLabel("Resumen de métricas QA registradas en backend")
        desc.setStyleSheet("color: #64748b;")

        self.refresh_metrics_btn = QPushButton("Actualizar métricas")
        self.refresh_metrics_btn.clicked.connect(self.on_refresh_metrics)

        self.metrics_output = QPlainTextEdit()
        self.metrics_output.setReadOnly(True)

        layout.addWidget(title)
        layout.addWidget(desc)
        layout.addWidget(self.refresh_metrics_btn)
        layout.addWidget(self.metrics_output)
        return page

    def _build_projects_view(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)

        title = QLabel("Proyectos")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        desc = QLabel("Selecciona un proyecto para cargar pruebas del módulo de Pruebas Automatizadas")
        desc.setStyleSheet("color: #64748b;")

        lists_row = QHBoxLayout()

        projects_col = QVBoxLayout()
        projects_col.addWidget(QLabel("Lista de proyectos"))
        self.projects_list = QListWidget()
        self.projects_list.itemSelectionChanged.connect(self.on_project_selected)
        projects_col.addWidget(self.projects_list)

        tests_col = QVBoxLayout()
        tests_col.addWidget(QLabel("Pruebas automáticas (Legacy + Workflows visuales)"))
        self.tests_list = QListWidget()
        tests_col.addWidget(self.tests_list)

        lists_row.addLayout(projects_col)
        lists_row.addLayout(tests_col)

        actions_row = QHBoxLayout()
        self.refresh_projects_btn = QPushButton("Actualizar proyectos")
        self.refresh_projects_btn.clicked.connect(self.on_refresh_projects)

        runs_label = QLabel("Número de pruebas")
        self.runs_input = QLineEdit("1")
        self.runs_input.setFixedWidth(72)
        self.runs_input.setValidator(QIntValidator(1, 999, self))
        self.runs_input.setToolTip("Cantidad de corridas a ejecutar (por defecto 1)")

        self.run_btn = QPushButton("Ejecutar prueba seleccionada")
        self.run_btn.clicked.connect(self.on_run_clicked)
        actions_row.addWidget(self.refresh_projects_btn)
        actions_row.addWidget(runs_label)
        actions_row.addWidget(self.runs_input)
        actions_row.addWidget(self.run_btn)

        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.timeline = QPlainTextEdit()
        self.timeline.setReadOnly(True)

        layout.addWidget(title)
        layout.addWidget(desc)
        layout.addLayout(lists_row)
        layout.addLayout(actions_row)
        layout.addWidget(QLabel("Logs en tiempo real"))
        layout.addWidget(self.logs)
        layout.addWidget(QLabel("Timeline de ejecución"))
        layout.addWidget(self.timeline)
        return page

    def _build_settings_view(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        title = QLabel("Configuración")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")

        api_label = QLabel(f"API Base URL: {settings.api_base_url}")
        api_label.setWordWrap(True)

        captcha_status = "Configurado" if settings.two_captcha_api_key else "No configurado"
        captcha_label = QLabel(f"2Captcha: {captcha_status}")

        info = QLabel("Actualiza variables en el archivo .env del desktop-runner para cambiar estos valores.")
        info.setWordWrap(True)
        info.setStyleSheet("color: #64748b;")

        layout.addWidget(title)
        layout.addSpacing(10)
        layout.addWidget(api_label)
        layout.addWidget(captcha_label)
        layout.addWidget(info)
        layout.addStretch()
        return page

    def on_login_clicked(self) -> None:
        email = self.email_input.text().strip()
        password = self.password_input.text().strip()
        if not email or not password:
            self.login_status.setText("Completa usuario y clave")
            return

        self.login_btn.setEnabled(False)
        self.login_status.setStyleSheet("color: #334155;")
        self.login_status.setText("Validando credenciales...")

        try:
            ok = asyncio.run(self.api.login(email, password))
        except Exception as exc:  # noqa: BLE001
            ok = False
            self.login_status.setStyleSheet("color: #dc2626;")
            self.login_status.setText(f"Error de conexión: {exc}")

        self.login_btn.setEnabled(True)
        if not ok:
            self.login_status.setStyleSheet("color: #dc2626;")
            self.login_status.setText("Login fallido")
            return

        self.login_status.setText("")
        self.auth_stack.setCurrentIndex(1)
        self.home_stack.setCurrentIndex(self.VIEW_PROJECTS)
        self.load_home_data()

    def load_home_data(self) -> None:
        self.on_refresh_projects()
        self.on_refresh_metrics()

    def on_refresh_projects(self) -> None:
        try:
            projects = asyncio.run(self.api.list_projects())
        except Exception as exc:  # noqa: BLE001
            self.logs.appendPlainText(f"Error cargando proyectos: {exc}")
            return

        self.current_projects = projects
        self.projects_list.clear()
        self.tests_list.clear()
        self.current_project_id = None

        for project in projects:
            item = QListWidgetItem(project.get("name", "Proyecto sin nombre"))
            item.setData(Qt.ItemDataRole.UserRole, project.get("id"))
            self.projects_list.addItem(item)

        self.logs.appendPlainText(f"Proyectos cargados: {len(projects)}")

    def on_project_selected(self) -> None:
        item = self.projects_list.currentItem()
        if not item:
            return

        project_id = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(project_id, int):
            return

        self.current_project_id = project_id
        self.tests_list.clear()
        self.logs.appendPlainText(f"Cargando pruebas automáticas del proyecto #{project_id}...")

        try:
            login_tests = asyncio.run(self.api.get_login_configs(project_id))
            visual_workflows = asyncio.run(self.api.list_workflows(project_id))
        except Exception as exc:  # noqa: BLE001
            self.logs.appendPlainText(f"Error cargando pruebas del proyecto: {exc}")
            return

        total = 0

        for test in login_tests:
            legacy_name = test.get("name") or f"Login Config #{test.get('id', '?')}"
            label = f"[Legacy] {legacy_name}"
            payload = {**test, "execution_mode": "legacy"}
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, payload)
            self.tests_list.addItem(item)
            total += 1

        for workflow in visual_workflows:
            definition = workflow.get("definition") if isinstance(workflow.get("definition"), dict) else {}
            runtime_settings = workflow.get("runtime_settings") if isinstance(workflow.get("runtime_settings"), dict) else {}
            payload = {
                "id": workflow.get("id"),
                "project_id": project_id,
                "name": workflow.get("name") or f"Workflow #{workflow.get('id', '?')}",
                "workflow_definition": definition,
                "runtime_settings": runtime_settings,
                "capture_screenshots": bool(runtime_settings.get("capture_screenshots", False)),
                "capture_video": bool(runtime_settings.get("capture_video", False)),
                "execution_mode": "workflow",
            }
            label = f"[Workflow] {payload['name']}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, payload)
            self.tests_list.addItem(item)
            total += 1

        self.logs.appendPlainText(f"Pruebas automáticas encontradas: {total}")

    def on_refresh_metrics(self) -> None:
        self.metrics_output.clear()
        try:
            metrics = asyncio.run(self.api.list_qa_metrics())
        except Exception as exc:  # noqa: BLE001
            self.metrics_output.appendPlainText(f"Error cargando métricas: {exc}")
            return

        if not metrics:
            self.metrics_output.appendPlainText("No hay métricas registradas")
            return

        for m in metrics:
            self.metrics_output.appendPlainText(
                f"Proyecto {m.get('project_id')} | Exec: {m.get('executions_total')} | "
                f"Pass: {m.get('pass_total')} | Fail: {m.get('fail_total')} | "
                f"DefectDensity: {m.get('defect_density')} | Flaky: {m.get('flaky_rate')}"
            )

    def on_run_clicked(self) -> None:
        if self.worker and self.worker.isRunning():
            self.logs.appendPlainText("Ya hay una ejecución en curso. Espera a que termine.")
            return

        item = self.tests_list.currentItem()
        if not item:
            self.logs.appendPlainText("Selecciona una prueba automática")
            return

        payload = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(payload, dict):
            self.logs.appendPlainText("No se encontró configuración válida para ejecutar")
            return

        test_name = payload.get("name", "login-test")
        execution_mode = payload.get("execution_mode", "legacy")
        run_count_raw = (self.runs_input.text() or "1").strip()
        try:
            run_count = max(1, int(run_count_raw))
        except ValueError:
            self.logs.appendPlainText("Número de pruebas inválido. Usa un entero >= 1.")
            return

        self.logs.appendPlainText(f"Iniciando ejecución ({execution_mode}): {test_name}")
        self.logs.appendPlainText(f"Número de pruebas configurado: {run_count}")
        self.timeline.appendPlainText("{event: execution_requested}")
        self.run_btn.setEnabled(False)
        self.runs_input.setEnabled(False)

        self.worker = AsyncWorker(
            test_name=test_name,
            login_config=payload,
            api_client=self.api,
            run_count=run_count,
        )
        self.worker.message.connect(self.logs.appendPlainText)
        self.worker.timeline.connect(self.timeline.appendPlainText)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()

    def on_worker_finished(self) -> None:
        self.run_btn.setEnabled(True)
        self.runs_input.setEnabled(True)
        self.logs.appendPlainText("Ejecución finalizada")
        self.worker = None

    def logout(self) -> None:
        if self.worker and self.worker.isRunning():
            self.logs.appendPlainText("Espera a que termine la ejecución antes de cerrar sesión")
            return

        self.api.token = None
        self.email_input.clear()
        self.password_input.clear()
        self.login_status.setText("")
        self.projects_list.clear()
        self.tests_list.clear()
        self.metrics_output.clear()
        self.logs.clear()
        self.timeline.clear()
        self.current_project_id = None
        self.auth_stack.setCurrentIndex(0)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.worker and self.worker.isRunning():
            self.logs.appendPlainText("Esperando a que finalice la ejecución antes de cerrar...")
            self.worker.wait(15000)
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(app_icon())
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
