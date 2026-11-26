"""Adaptador de modelos GLM hecho para Vortex AI Desktop."""

import base64
import json
from functools import lru_cache

import requests
from google import genai
from google.genai import types

from agent_core.provider_keys import GLM_BASE, GLM_FREE_MODELS, credentials


def json_schema(value):
    if isinstance(value, list):
        return [json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: json_schema(item) for key, item in value.items() if key not in ("property_ordering", "propertyOrdering")}
    if isinstance(result.get("type"), str):
        result["type"] = result["type"].lower()
    return result


@lru_cache(maxsize=128)
def tool_schema(function):
    with genai.Client(api_key="schema-conversion-only") as client:
        declaration = types.FunctionDeclaration.from_callable(client=client._api_client, callable=function, use_json_schema=True)
    parameters = declaration.parameters_json_schema or (declaration.parameters.model_dump(mode="json", exclude_none=True) if declaration.parameters else {"type": "object", "properties": {}})
    return {"type": "function", "function": {"name": declaration.name, "description": declaration.description or "", "parameters": json_schema(parameters)}}


def convert_messages(contents, system_instruction):
    messages = [{"role": "system", "content": str(system_instruction or "")}]
    pending = {}
    image_present = False
    for content in contents:
        text = []
        images = []
        calls = []
        responses = []
        for part in content.parts or []:
            if part.text:
                text.append(part.text)
            if part.inline_data:
                mime = part.inline_data.mime_type or ""
                if not mime.startswith("image/"):
                    raise ValueError("GLM no admite este adjunto en el modo de programación. Selecciona Gemini.")
                image_present = True
                data = base64.b64encode(part.inline_data.data).decode()
                images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
            if part.file_data:
                raise ValueError("El adjunto requiere un modelo Gemini compatible")
            if part.function_call:
                fc = part.function_call
                call_id = fc.id or f"call_{len(messages)}_{len(calls)}"
                calls.append({"id": call_id, "type": "function", "function": {"name": fc.name, "arguments": json.dumps(fc.args or {})}})
                pending.setdefault(fc.name, []).append(call_id)
            if part.function_response:
                response = part.function_response
                ids = pending.get(response.name, [])
                if not ids:
                    raise ValueError("La respuesta de herramienta no tiene una llamada asociada")
                responses.append({"role": "tool", "tool_call_id": ids.pop(0), "content": json.dumps(response.response or {}, ensure_ascii=False)})
        messages.extend(responses)
        if calls:
            messages.append({"role": "assistant", "content": "\n".join(text) or None, "tool_calls": calls})
        elif text or images:
            body = "\n".join(text)
            if images:
                body = [{"type": "text", "text": body or "Analiza la imagen"}, *images]
            messages.append({"role": "assistant" if content.role == "model" else "user", "content": body})
    return messages, image_present


def generate_glm(model, contents, config):
    model = model.removeprefix("glm/")
    if model not in GLM_FREE_MODELS:
        raise ValueError("Modelo GLM excluido: esta aplicación usa solo GLM gratuito")
    messages, has_images = convert_messages(contents, config.system_instruction)
    if has_images and "v" not in model and not model.startswith("glm-5"):
        raise ValueError("Este modelo GLM es de texto. La imagen requiere un modelo con visión.")
    tools = [tool_schema(function) for function in config.tools or [] if callable(function)]
    payload = {"model": model, "messages": messages, "max_tokens": config.max_output_tokens or 8192, "temperature": config.temperature if config.temperature is not None else 0.3}
    if tools:
        payload.update(tools=tools, tool_choice="auto")
    keys = credentials("glm")
    if not keys:
        raise ValueError("Agrega una clave de Z.ai en Configuración")
    for index, (_, key, _) in enumerate(keys):
        response = requests.post(GLM_BASE + "/chat/completions", headers={"Authorization": "Bearer " + key}, json=payload, timeout=35)
        if response.status_code in (401, 403, 429) and index < len(keys) - 1:
            continue
        if not response.ok:
            try:
                error_code = str(response.json().get("error", {}).get("code", ""))
            except (ValueError, AttributeError):
                error_code = ""
            if error_code == "1305":
                raise ValueError("GLM HTTP 503: servicio temporalmente saturado (1305)")
            if error_code == "1113":
                raise ValueError("GLM HTTP 429: quota limit: 0; sin saldo para el modelo")
            raise ValueError(f"GLM HTTP {response.status_code}: solicitud no disponible")
        data = response.json()
        message = data.get("choices", [{}])[0].get("message", {})
        parts = []
        if message.get("content"):
            parts.append(types.Part.from_text(text=message["content"]))
        for call in message.get("tool_calls", []):
            function = call["function"]
            arguments = json.loads(function["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError("GLM devolvió argumentos de herramienta inválidos")
            parts.append(types.Part(function_call=types.FunctionCall(id=call["id"], name=function["name"], args=arguments)))
        if not parts:
            raise ValueError("GLM devolvió una respuesta vacía")
        usage = data.get("usage", {})
        return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=parts))], usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=usage.get("prompt_tokens", 0), candidates_token_count=usage.get("completion_tokens", 0), total_token_count=usage.get("total_tokens", 0)))
