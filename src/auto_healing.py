from openai import AzureOpenAI

from config import settings


class AutoHealingEngine:
    def __init__(self) -> None:
        if settings.openai_api_key and settings.openai_base_url:
            self.client = AzureOpenAI(
                api_key=settings.openai_api_key,
                api_version=settings.openai_api_version,
                azure_endpoint=settings.openai_base_url
            )
        else:
            self.client = None

    async def suggest_fix(self, failing_step: str, error_message: str) -> str:
        if not self.client:
            return "Azure OpenAI no configurado. No se puede sugerir auto-healing."

        prompt = (
            "Eres un experto en Playwright. "
            "Sugiere una correccion concreta para este paso fallido.\n"
            f"Paso: {failing_step}\n"
            f"Error: {error_message}\n"
        )
        response = self.client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content or ""
