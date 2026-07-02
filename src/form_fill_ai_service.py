from __future__ import annotations

import json
import re
import secrets
from html import unescape
from typing import Any

from openai import AzureOpenAI

from config import settings


class FormFillAiService:
    def __init__(self) -> None:
        if settings.openai_api_key and settings.openai_base_url:
            self.client = AzureOpenAI(
                api_key=settings.openai_api_key,
                api_version=settings.openai_api_version,
                azure_endpoint=settings.openai_base_url,
            )
        else:
            self.client = None

    def is_enabled(self) -> bool:
        return self.client is not None

    async def generate_fields(
        self,
        *,
        html_context: str,
        page_url: str,
        page_title: str,
        node_label: str,
        catalog_entries: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        payload = await self.generate_fields_payload(
            html_context=html_context,
            catalog_entries=catalog_entries,
            page_url=page_url,
            page_title=page_title,
            node_label=node_label,
        )
        fields = payload.get("fields")
        return fields if isinstance(fields, list) else []

    async def generate_fields_payload(
        self,
        *,
        html_context: str,
        page_url: str,
        page_title: str,
        node_label: str,
        catalog_entries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self.client:
            return {
                "fields": [],
                "submit_action": {"intent": "none", "text": ""},
                "prompt": "",
                "raw_response": "",
                "model_name": "",
                "catalog_context": "",
            }

        catalog_context = self._build_catalog_context(
            html_context=html_context,
            catalog_entries=catalog_entries,
        )
        variation_seed = secrets.randbelow(10_000_000)
        import random

        # Semilla que ya utilizas para variación
        rnd = random.Random(str(variation_seed))

        # Identificador de 12 dígitos
        generated_record_id = ''.join(
            str(rnd.randint(0, 9))
            for _ in range(12)
        )

        # Evitar algunos patrones triviales
        while (
            generated_record_id in {
                "000000000000",
                "111111111111",
                "123456789012",
                "987654321098",
                "999999999999",
            }
        ):
            generated_record_id = ''.join(
                str(rnd.randint(0, 9))
                for _ in range(12)
            )

        # Correo opcional derivado de la semilla
        generated_email = (
            f"usuario_{abs(hash(str(variation_seed))) % 100000}"
            "@example.com"
        )

        # Teléfono opcional
        generated_phone = (
            "55" +
            ''.join(str(rnd.randint(0, 9)) for _ in range(8))
        )   
        prompt = (
            "Eres un experto en Playwright y automatizacion de formularios. "
            "Analiza el HTML de un formulario y devuelve SOLO un JSON valido con esta estructura exacta:\n"
            "{\n"
            '  "fields": [\n'
            "    {\n"
            '      "field_name": "nombre legible del campo",\n'
            '      "selector_type": "label|name|placeholder|css|xpath",\n'
            '      "selector": "selector o texto para localizar el input",\n'
            '      "value": "valor sugerido a llenar"\n'
            "    }\n"
            "  ],\n"
            '  "submit_action": {\n'
            '    "intent": "save|continue|submit|next|finish|confirm|none",\n'
            '    "text": "texto exacto del boton final visible (ej: Continuar, Guardar, Enviar)"\n'
            "  }\n"
            "}\n"

            "Datos generados para esta ejecucion:\n"
            f"- record_id: {generated_record_id}\n"
            f"- email: {generated_email}\n"
            f"- phone: {generated_phone}\n"

            "Reglas:\n"

            "- Devuelve al menos un campo si el formulario tiene inputs visibles.\n"
            "- Usa selector_type='css' o 'xpath' solo si hace falta.\n"
            "- Identifica cada campo por su etiqueta visible, aria-label, texto asociado o name.\n"
            "- Prefiere selector_type='label' cuando exista etiqueta; usa 'name' como segunda opcion y 'placeholder' solo como ultimo recurso.\n"
            "- Interpreta el tipo de dato segun la etiqueta, el placeholder, el name y el contexto del formulario.\n"

            "- Si el campo corresponde a identificadores de registro (Numero de Cliente, Numero de Registro, Folio, ID Cliente, ID Registro, No. Cliente, N° Cliente, Customer Id, Customer Number, Client Id, Registro), DEBES usar exactamente el valor proporcionado en record_id.\n"
            "- No generes otro identificador.\n"
            "- No modifiques record_id.\n"
            "- No agregues letras, espacios, prefijos ni sufijos.\n"
            "- Usa exactamente los 12 digitos proporcionados.\n"

            "- Si el campo representa correo electronico utiliza preferentemente el valor email proporcionado.\n"
            "- Si el campo representa telefono utiliza preferentemente el valor phone proporcionado.\n"

            "- Genera valores NO vacios y plausibles segun la semantica del campo.\n"
            "- Si el campo representa una fecha, devuelve una fecha valida con el formato esperado por el control.\n"
            "- Si el campo representa regimen fiscal, RFC, correo, telefono, monto o numero, usa un valor compatible con ese tipo de dato.\n"

            "- Para nombres de personas genera nombres completos plausibles y variados en español.\n"
            "- Para empresas o razones sociales genera nombres plausibles y variados.\n"
            "- Para direcciones genera valores plausibles y variados.\n"
            "- Para observaciones o comentarios genera textos breves coherentes.\n"

            "- Para campos select, combobox, listbox o catalogos, el value DEBE salir exactamente de una opcion/catalogo presente en el HTML del catalogo asociado a ese componente.\n"
            "- Si un campo parece de fecha (etiqueta contiene Fecha o el control es input/date/text con patron de fecha), tratalo como input de texto/fecha y NO como catalogo aunque tenga role='combobox'.\n"
            "- Para combos MUI detectados como div.MuiSelect-select con role='combobox', el value DEBE ser exactamente uno de los textos visibles del catalogo desplegado de ese combo.\n"
            "- No inventes opciones para combos.\n"
            "- Si existe un catalogo de opciones para un componente, elige literalmente uno de sus valores visibles.\n"
            "- Si un campo es combobox/select y no hay catalogo visible para ese mismo componente, no mezcles valores de otros catalogos.\n"
            "- Cuando un campo tenga catalogo asociado, usa ese catalogo y no mezcles opciones de otros componentes.\n"
            "- Cuando un campo tenga opciones en un <select>, <option>, listas tipo <ul><li>, [role=listbox], [role=option], o estructuras equivalentes con divs, usa uno de esos textos exactos como value.\n"
            "- Si un catalogo pertenece a un campo como Plaza, Regimen Fiscal, Estado, Municipio o similar, selecciona solo una opcion que exista textual y exactamente en el catalogo de ese campo.\n"
            "- La seccion 'Catalogos por componente' esta anidada por etiqueta de campo; para cada campo select/combobox/listbox DEBES usar exclusivamente opciones de la misma etiqueta.\n"

            "- No uses el placeholder como valor; el placeholder solo sirve para inferir formato.\n"
            "- No devuelvas values vacios para campos visibles obligatorios.\n"

            "- Evita valores genericos como Juan Perez, Maria Garcia, usuario@example.com, test@test.com, correo@correo.com.\n"

            "- La semilla de variacion DEBE influir en los datos generados.\n"
            "- Si existen varias respuestas validas para un campo, selecciona una alternativa distinta influenciada por la semilla.\n"
            "- Prioriza diversidad sobre repeticion cuando el contexto lo permita.\n"

            "- Identifica el boton de accion final del formulario y devuelvelo en submit_action.\n"
            "- submit_action.text debe ser exactamente el texto visible del boton final.\n"
            "- Si no existe boton final claro, usa submit_action.intent='none' y submit_action.text=''.\n"

            "- Devuelve exclusivamente JSON valido.\n"
            "- No incluyas markdown.\n"
            "- No incluyas explicaciones.\n"
            "- No incluyas texto adicional.\n"

            f"- Semilla de variacion: {variation_seed}\n"
            f"- URL: {page_url}\n"
            f"- Titulo: {page_title}\n"
            f"- Nodo: {node_label}\n"
            f"- Catalogos por componente:\n{catalog_context}\n"
            f"- HTML del formulario (sin catalogos mezclados):\n{html_context}\n"
        )

        response = self.client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.35,
        )
        content = response.choices[0].message.content or ""
        parsed_response = self._parse_ai_response(content)
        fields = parsed_response.get("fields") if isinstance(parsed_response, dict) else []
        submit_action = parsed_response.get("submit_action") if isinstance(parsed_response, dict) else None
        if not isinstance(fields, list):
            fields = []
        if not isinstance(submit_action, dict):
            submit_action = {"intent": "none", "text": ""}

        fields = self._align_fields_with_catalogs(fields, catalog_entries or [])
        fields = self._enforce_numeric_identifiers(fields)
        return {
            "fields": fields,
            "submit_action": submit_action,
            "prompt": prompt,
            "raw_response": content,
            "model_name": settings.openai_model,
            "catalog_context": catalog_context,
        }

    def _parse_ai_response(self, content: str) -> dict[str, Any]:
        raw_json = self._extract_json_block(content)
        if not raw_json:
            return {
                "fields": [],
                "submit_action": {"intent": "none", "text": ""},
            }

        try:
            parsed = json.loads(raw_json)
        except Exception:
            return {
                "fields": [],
                "submit_action": {"intent": "none", "text": ""},
            }

        if not isinstance(parsed, dict):
            return {
                "fields": [],
                "submit_action": {"intent": "none", "text": ""},
            }

        fields = parsed.get("fields")
        if not isinstance(fields, list):
            fields = []

        normalized: list[dict[str, Any]] = []
        for item in fields:
            if not isinstance(item, dict):
                continue
            selector_type = str(item.get("selector_type", "label")).strip().lower()
            if selector_type not in {"label", "name", "placeholder", "css", "xpath"}:
                selector_type = "label"
            normalized.append(
                {
                    "field_name": str(item.get("field_name", "")).strip(),
                    "selector_type": selector_type,
                    "selector": str(item.get("selector", "")).strip(),
                    "value": str(item.get("value", "")).strip(),
                }
            )

        submit_action_raw = parsed.get("submit_action")
        submit_intent = "none"
        submit_text = ""
        if isinstance(submit_action_raw, dict):
            raw_intent = str(submit_action_raw.get("intent", "none")).strip().lower()
            valid_intents = {"save", "continue", "submit", "next", "finish", "confirm", "none"}
            submit_intent = raw_intent if raw_intent in valid_intents else "none"
            submit_text = str(submit_action_raw.get("text", "")).strip()

        return {
            "fields": normalized,
            "submit_action": {
                "intent": submit_intent,
                "text": submit_text,
            },
        }

    def _extract_json_block(self, content: str) -> str:
        stripped = content.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
            stripped = re.sub(r"\s*```$", "", stripped)
        return stripped

    def _build_catalog_context(
        self,
        *,
        html_context: str,
        catalog_entries: list[dict[str, Any]] | None,
    ) -> str:
        structured = self._format_catalog_entries(catalog_entries or [])
        if structured:
            return structured
        return self._extract_catalog_context_from_html(html_context)

    def _format_catalog_entries(self, entries: list[dict[str, Any]]) -> str:
        source_counter: dict[str, int] = {}
        grouped_by_label: dict[str, list[dict[str, Any]]] = {}

        for index, item in enumerate(entries, start=1):
            field_label = self._derive_catalog_label(item, index)
            trigger_html = str(item.get("trigger_html", "")).strip()
            catalog_html = str(item.get("catalog_html", "")).strip()
            source = str(item.get("source", "")).strip() or "desconocido"
            if not catalog_html:
                continue

            source_counter[source] = source_counter.get(source, 0) + 1
            catalog_key = f"{source}:{source_counter[source]}:{field_label}"

            options = self._extract_options_from_catalog_html(catalog_html)
            option_text = ", ".join(options[:25]) if options else "Sin opciones detectadas"
            grouped_by_label.setdefault(field_label, []).append(
                {
                    "catalog_key": catalog_key,
                    "source": source,
                    "option_text": option_text,
                    "trigger_html": trigger_html[:1200],
                    "catalog_html": catalog_html[:4000],
                }
            )
        if not grouped_by_label:
            return ""

        sections: list[str] = []
        for label in list(grouped_by_label.keys())[:25]:
            catalogs = grouped_by_label[label]
            block = [
                f"EtiquetaCampo: {label}",
                f"CatalogosAsociados: {len(catalogs)}",
            ]
            for catalog in catalogs[:5]:
                block.append(f"CatalogoID: {catalog['catalog_key']}")
                block.append(f"TipoCatalogo: {catalog['source']}")
                block.append(f"OpcionesVisibles: {catalog['option_text']}")
                if catalog["trigger_html"]:
                    block.append(f"TriggerHTML: {catalog['trigger_html']}")
                block.append(f"CatalogoHTML: {catalog['catalog_html']}")
            sections.append("\n".join(block))

        return "\n\n".join(sections)

    def _derive_catalog_label(self, item: dict[str, Any], index: int) -> str:
        raw_label = str(item.get("field_label", "")).strip()
        if raw_label and not self._is_noise_label(raw_label):
            return raw_label

        trigger_html = str(item.get("trigger_html", ""))
        catalog_html = str(item.get("catalog_html", ""))
        for source_html in (trigger_html, catalog_html):
            for attr_name in ("aria-label", "name", "id", "data-testid"):
                match = re.search(rf'{attr_name}=["\']([^"\']+)["\']', source_html, re.IGNORECASE)
                candidate = str(match.group(1)).strip() if match else ""
                if candidate and not self._is_noise_label(candidate):
                    return candidate

        source = str(item.get("source", "")).strip() or "catalogo"
        return f"{source}_{index}"

    def _is_noise_label(self, label: str) -> bool:
        normalized = self._normalize_text(label)
        if not normalized:
            return True
        if normalized in {"listbox", "select", "combo", "catalogo", "catalog"}:
            return True
        if label.startswith(":") and "-label" in label:
            return True
        return bool(re.match(r"^:?[_a-z0-9-]+:?-label$", label, re.IGNORECASE))

    def _align_fields_with_catalogs(
        self,
        fields: list[dict[str, Any]],
        entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not fields or not entries:
            return fields

        catalogs: list[dict[str, Any]] = []
        for index, item in enumerate(entries, start=1):
            label = self._derive_catalog_label(item, index)
            source = str(item.get("source", "")).strip() or "desconocido"
            catalog_html = str(item.get("catalog_html", "")).strip()
            if not label or not catalog_html:
                continue
            options = self._extract_options_from_catalog_html(catalog_html)
            if not options:
                continue
            catalogs.append(
                {
                    "label": label,
                    "source": source,
                    "options": options,
                    "label_norm": self._normalize_text(label),
                }
            )

        if not catalogs:
            return fields

        aligned: list[dict[str, Any]] = []
        for field in fields:
            if not isinstance(field, dict):
                continue

            updated = dict(field)
            if not self._field_prefers_catalog(updated):
                aligned.append(updated)
                continue

            best_catalog = self._find_best_catalog_for_field(updated, catalogs)
            if not best_catalog:
                aligned.append(updated)
                continue

            current_value = str(updated.get("value", "")).strip()
            options = best_catalog.get("options", [])
            resolved = self._resolve_catalog_value(current_value, options)
            if resolved:
                updated["value"] = resolved

            aligned.append(updated)

        return aligned

    def _find_best_catalog_for_field(
        self,
        field: dict[str, Any],
        catalogs: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        hint = " ".join(
            [
                str(field.get("field_name", "")),
                str(field.get("selector", "")),
            ]
        )
        hint_norm = self._normalize_text(hint)
        if not hint_norm:
            return None

        hint_tokens = set(token for token in hint_norm.split(" ") if token)
        best: dict[str, Any] | None = None
        best_score = 0

        for catalog in catalogs:
            label_norm = str(catalog.get("label_norm", "")).strip()
            if not label_norm:
                continue

            score = 0
            if label_norm in hint_norm or hint_norm in label_norm:
                score += 5

            label_tokens = set(token for token in label_norm.split(" ") if token)
            overlap = len(hint_tokens.intersection(label_tokens))
            score += overlap * 2

            if overlap == 0 and score < 5:
                continue

            if score > best_score:
                best_score = score
                best = catalog

        return best

    def _field_prefers_catalog(self, field: dict[str, Any]) -> bool:
        selector_type = str(field.get("selector_type", "")).strip().lower()
        hint = " ".join(
            [
                str(field.get("field_name", "")),
                str(field.get("selector", "")),
            ]
        ).lower()
        if selector_type in {"css", "xpath"} and any(token in hint for token in ["select", "combobox", "listbox", "mui"]):
            return True
        return any(
            token in hint
            for token in [
                "select",
                "seleccion",
                "selección",
                "combobox",
                "listbox",
                "catalogo",
                "catálogo",
                "regimen",
                "régimen",
                "estado",
                "municipio",
                "plaza",
            ]
        )

    def _resolve_catalog_value(self, current_value: str, options: list[str]) -> str:
        if not options:
            return current_value

        valid_options: list[str] = []
        for option in options:
            norm = self._normalize_text(option)
            if norm and norm not in {"seleccione", "selecciona", "select", "--"}:
                valid_options.append(option)

        if current_value:
            current_norm = self._normalize_text(current_value)
            for option in options:
                if self._normalize_text(option) == current_norm:
                    return option

            # Match flexible para cuando IA devuelve un texto cercano pero no exacto.
            for option in options:
                option_norm = self._normalize_text(option)
                if current_norm and option_norm and (current_norm in option_norm or option_norm in current_norm):
                    return option

        if valid_options:
            return secrets.choice(valid_options)

        return secrets.choice(options)

    def _enforce_numeric_identifiers(self, fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not fields:
            return fields

        used_identifiers: set[str] = set()
        normalized_fields: list[dict[str, Any]] = []
        for item in fields:
            if not isinstance(item, dict):
                continue

            updated = dict(item)
            hint = " ".join([
                str(updated.get("field_name", "")),
                str(updated.get("selector", "")),
            ])

            if self._is_numeric_identifier_field(hint):
                current_value = str(updated.get("value", "")).strip()
                digits_only = re.sub(r"\D", "", current_value)
                if len(digits_only) == 12 and digits_only not in used_identifiers:
                    updated["value"] = digits_only
                    used_identifiers.add(digits_only)
                else:
                    generated = self._generate_random_12_digit_id()
                    while generated in used_identifiers:
                        generated = self._generate_random_12_digit_id()
                    updated["value"] = generated
                    used_identifiers.add(generated)

            normalized_fields.append(updated)

        return normalized_fields

    def _is_numeric_identifier_field(self, hint: str) -> bool:
        normalized = self._normalize_text(hint)
        if not normalized:
            return False

        keywords = [
            "numero de cliente",
            "numero cliente",
            "no cliente",
            "n cliente",
            "id cliente",
            "identificador cliente",
            "numero de registro",
            "numero registro",
            "id registro",
            "identificador registro",
            "folio",
            "consecutivo",
            "expediente",
        ]
        return any(keyword in normalized for keyword in keywords)

    def _generate_random_12_digit_id(self) -> str:
        return "".join(str(secrets.randbelow(10)) for _ in range(12))

    def _normalize_text(self, value: str) -> str:
        text = self._clean_catalog_text(value or "")
        text = text.lower()
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _extract_catalog_context_from_html(self, html_context: str) -> str:
        html = html_context or ""
        if not html.strip():
            return "Sin catalogos detectados"

        catalog_lines: list[str] = []
        select_re = re.compile(
            r"<select\b(?P<attrs>[^>]*)>(?P<body>.*?)</select>",
            re.IGNORECASE | re.DOTALL,
        )
        listbox_re = re.compile(
            r'<(?:ul|div|ol)\b(?P<attrs>[^>]*)role=["\']listbox["\'][^>]*>(?P<body>.*?)</(?:ul|div|ol)>',
            re.IGNORECASE | re.DOTALL,
        )

        for idx, match in enumerate(select_re.finditer(html), start=1):
            identifier = self._extract_catalog_identifier(match.group("attrs") or "", f"select_{idx}")
            options = self._extract_options_from_catalog_html(match.group(0))
            if options:
                catalog_lines.append(f"Componente: {identifier}\nOpciones visibles: {', '.join(options[:25])}")

        for idx, match in enumerate(listbox_re.finditer(html), start=1):
            identifier = self._extract_catalog_identifier(match.group("attrs") or "", f"listbox_{idx}")
            options = self._extract_options_from_catalog_html(match.group(0))
            if options:
                catalog_lines.append(f"Componente: {identifier}\nOpciones visibles: {', '.join(options[:25])}")

        if not catalog_lines:
            return "Sin catalogos detectados"
        return "\n\n".join(catalog_lines[:25])

    def _extract_catalog_identifier(self, attrs: str, fallback: str) -> str:
        match = re.search(
            r'(?:aria-label|id|name|aria-labelledby|data-testid)=["\']([^"\']{1,80})["\']',
            attrs or "",
            re.IGNORECASE,
        )
        return str(match.group(1)).strip() if match else fallback

    def _extract_options_from_catalog_html(self, catalog_html: str) -> list[str]:
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
                value = self._extract_role_option_value(attrs, body)
                if value:
                    options.append(value)
            if options:
                break

        return list(dict.fromkeys(option for option in options if option))[:50]

    def _clean_catalog_text(self, raw_text: str) -> str:
        text = re.sub(r"<[^>]+>", " ", raw_text or "")
        text = unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _extract_role_option_value(self, attrs: str, body: str) -> str:
        data_value_match = re.search(r'data-value=["\']([^"\']+)["\']', attrs or "", re.IGNORECASE)
        if data_value_match:
            return self._clean_catalog_text(data_value_match.group(1))
        return self._clean_catalog_text(body)
