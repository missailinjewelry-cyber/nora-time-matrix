"""Звернення до ШІ через OpenAI-сумісний API (chat/completions).

Бот пробує моделі по черзі: головну (OPENAI_MODEL), потім запасні
(OPENAI_FALLBACK_MODELS, можна кілька через кому). Перша, що відповіла, перемагає.
"""
import asyncio
import base64
import logging
import os
import time

import httpx

log = logging.getLogger("Nora")

BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
API_KEY = os.getenv("OPENAI_API_KEY", "")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")


def _build_models():
    raw = os.getenv("OPENAI_FALLBACK_MODELS") or os.getenv("OPENAI_FALLBACK_MODEL") or ""
    models = [MODEL]
    for name in raw.split(","):
        name = name.strip()
        if name and name not in models:
            models.append(name)
    return models


MODELS = _build_models()
# Деталізація зображень: auto / low / high. high повільніша й дорожча.
IMAGE_DETAIL = os.getenv("IMAGE_DETAIL", "auto").strip().lower()
if IMAGE_DETAIL not in ("auto", "low", "high"):
    IMAGE_DETAIL = "auto"
# Швидкість міркування для моделей, що її підтримують: minimal / low / medium / high.
REASONING = os.getenv("REASONING_EFFORT", "").strip().lower()
# SHOW_MODEL=1 додає під відповіддю, яка модель відповіла й за скільки секунд.
SHOW_MODEL = os.getenv("SHOW_MODEL", "").strip().lower() in ("1", "true", "yes", "on")
TIMEOUT = float(os.getenv("AI_TIMEOUT_SEC", "90") or 90)
RETRY_STATUS = {429, 500, 502, 503, 504}
ROUNDS = 2  # скільки разів пройти весь список моделей
ROUND_PAUSES = [5, 10]  # пауза (с) після кожного неуспішного кола


class AIError(Exception):
    """Помилка ШІ з коротким повідомленням, яке можна показати користувачці."""


def image_mime(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _content(parts):
    """parts: список {"text": str} або {"image": bytes}."""
    content = []
    for part in parts:
        if "text" in part:
            content.append({"type": "text", "text": part["text"]})
        else:
            raw = part["image"]
            b64 = base64.b64encode(raw).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image_mime(raw)};base64,{b64}",
                    "detail": IMAGE_DETAIL,
                },
            })
    return content


async def ask_model(system, parts):
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": _content(parts)},
    ]
    n_images = sum(1 for p in parts if "image" in p)
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    started = time.monotonic()
    last_status = None
    dead = set()  # моделі, яких немає або вони недоступні для цього ключа
    no_reasoning = set()  # моделі, які не приймають reasoning_effort

    async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT, connect=15.0)) as client:
        for rnd in range(ROUNDS):
            for model in MODELS:
                if model in dead:
                    continue
                while True:  # повторюємо лише для відкату reasoning_effort
                    payload = {"model": model, "messages": messages}
                    if REASONING and model not in no_reasoning:
                        payload["reasoning_effort"] = REASONING
                    try:
                        r = await client.post(
                            f"{BASE_URL}/chat/completions", headers=headers, json=payload
                        )
                        last_status = r.status_code
                        if r.status_code in RETRY_STATUS:
                            log.warning("ШІ (%s) повернув %s, пробую наступну модель", model, r.status_code)
                            break
                        r.raise_for_status()
                        data = r.json()
                        text = (data["choices"][0]["message"].get("content") or "").strip()
                        if text:
                            elapsed = time.monotonic() - started
                            usage = data.get("usage") or {}
                            log.info(
                                "ШІ відповів за %.1f с (модель: %s, коло %d, зображень: %d, "
                                "токени: вхід %s / вихід %s)",
                                elapsed, model, rnd + 1, n_images,
                                usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"),
                            )
                            if SHOW_MODEL:
                                text += f"\n\n⚙️ {model}, {elapsed:.0f} с"
                            return text
                        log.warning("ШІ (%s) повернув порожню відповідь", model)
                        break
                    except httpx.HTTPStatusError as e:
                        code = e.response.status_code
                        last_status = code
                        log.error("ШІ (%s) помилка %s: %s", model, code, e.response.text[:300])
                        if code == 400 and "reasoning_effort" in payload:
                            no_reasoning.add(model)
                            log.warning("%s не приймає reasoning_effort, повторюю без нього", model)
                            continue
                        if code in (403, 404) and len(MODELS) > 1:
                            dead.add(model)
                            log.warning("Модель %s недоступна, виключаю зі списку", model)
                            break
                        raise AIError(
                            f"ШІ повернув помилку (код {code}). Подробиці в логах Railway."
                        )
                    except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                        log.warning("Збій запиту до ШІ (%s): %s", model, type(e).__name__)
                        break

            if len(dead) == len(MODELS):
                break
            if rnd < ROUNDS - 1:
                await asyncio.sleep(ROUND_PAUSES[rnd])

    if len(dead) == len(MODELS):
        log.error("Жодна модель недоступна: %s", ", ".join(MODELS))
        raise AIError(
            f"Жодна з моделей недоступна (код {last_status}). Перевір назви моделей у Railway."
        )
    log.error("ШІ недоступний (моделі: %s; останній статус: %s)", ", ".join(MODELS), last_status)
    raise AIError("Сервіс ШІ тимчасово перевантажений, спробуй за хвилину.")
