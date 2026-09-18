"""Calendar booking service — auto-books meetings on Calendly and Cal.com.

When a prospect shares their calendar link, this service:
1. Detects the provider (Calendly, Cal.com, etc.)
2. Fetches available time slots via the provider's API
3. Picks the best slot (next available during business hours)
4. Books the meeting with the user's name and email
5. Returns confirmation with the booked time

Supported providers:
- Calendly (internal scheduling API)
- Cal.com (public booking API)

Unsupported providers return the URL for manual booking.
"""

from __future__ import annotations

from ..textutil import first_name as _first_name

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(15.0, connect=10.0)

# Business hours for slot selection (user-local time)
_BIZ_START = 9   # 9 AM
_BIZ_END = 17    # 5 PM
_BIZ_DAYS = {0, 1, 2, 3, 4}  # Mon-Fri


def detect_provider(url: str) -> str:
    """Detect calendar provider from URL.

    Returns: 'calendly', 'calcom', or 'unknown'
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if "calendly.com" in host:
        return "calendly"
    if "cal.com" in host:
        return "calcom"
    return "unknown"


def _parse_calendly_path(url: str) -> tuple[str, str]:
    """Extract owner and event_slug from a Calendly URL.

    Calendly URLs: https://calendly.com/{owner}/{event_slug}
    Returns (owner, event_slug).
    """
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) >= 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _parse_calcom_path(url: str) -> tuple[str, str]:
    """Extract username and event_slug from a Cal.com URL.

    Cal.com URLs: https://cal.com/{username}/{event_slug}
    Returns (username, event_slug).
    """
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) >= 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _pick_best_slot(
    slots: list[dict[str, str]],
    user_tz: str = "UTC",
    prefer_after_hours: int = _BIZ_START,
    prefer_before_hours: int = _BIZ_END,
) -> dict[str, str] | None:
    """Pick the best available slot.

    Strategy:
    1. Prefer slots 2+ days from now (gives time for prep)
    2. Must be during business hours
    3. Prefer mid-morning (10-11 AM) or early afternoon (2-3 PM)
    4. Fall back to any business-hours slot
    """
    if not slots:
        return None

    now = datetime.now(timezone.utc)
    min_start = now + timedelta(days=1)  # At least tomorrow

    # Providers return UTC ("...Z"), but business hours and weekends are the
    # user's, not the provider's. An unknown zone falls back to UTC rather
    # than failing the booking outright.
    try:
        local_zone = ZoneInfo(user_tz or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown user timezone %r — scoring slots in UTC", user_tz)
        local_zone = timezone.utc

    scored: list[tuple[float, dict]] = []
    for slot in slots:
        start_str = slot.get("start") or slot.get("time") or ""
        if not start_str:
            continue
        try:
            start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue

        if start < min_start:
            continue

        local_start = start.astimezone(local_zone)
        hour = local_start.hour
        weekday = local_start.weekday()

        # Skip weekends
        if weekday not in _BIZ_DAYS:
            continue

        # Skip outside business hours
        if hour < prefer_after_hours or hour >= prefer_before_hours:
            continue

        # Score: prefer mid-morning and early afternoon
        score = 0.0
        if 10 <= hour <= 11:
            score = 10.0  # Best: mid-morning
        elif 14 <= hour <= 15:
            score = 8.0   # Good: early afternoon
        elif 9 <= hour <= 12:
            score = 6.0   # OK: morning
        elif 13 <= hour <= 16:
            score = 5.0   # OK: afternoon
        else:
            score = 2.0

        # Prefer 2-3 days out over tomorrow
        days_out = (start - now).days
        if 2 <= days_out <= 4:
            score += 3.0
        elif days_out == 1:
            score += 1.0

        scored.append((score, slot))

    if not scored:
        # Nothing scored — accept any slot that is at least far enough out,
        # even outside business hours. Order by time so we take the soonest.
        usable: list[tuple[datetime, dict[str, str]]] = []
        for slot in slots:
            start_str = slot.get("start") or slot.get("time") or ""
            if not start_str:
                continue
            try:
                start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if start >= min_start:
                usable.append((start, slot))
        if usable:
            usable.sort(key=lambda pair: pair[0])
            return usable[0][1]
        # Deliberately no last-resort `slots[0]`: it ignored every check above
        # and could return a slot in the past, which cannot be booked. The
        # caller reports "no suitable slot" on None, which is the honest answer.
        return None

    scored.sort(key=lambda x: -x[0])
    return scored[0][1]


async def _book_calendly(
    url: str,
    booker_name: str,
    booker_email: str,
    user_tz: str = "UTC",
) -> dict[str, Any]:
    """Book a meeting on Calendly using their internal scheduling API.

    Flow:
    1. GET event type info from the scheduling page
    2. GET available time slots for the next 2 weeks
    3. Pick the best slot
    4. POST booking with the user's details
    """
    owner, event_slug = _parse_calendly_path(url)
    if not owner:
        return {"success": False, "error": "Could not parse Calendly URL"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        # Step 1: Get event type UUID from the scheduling page API
        # Calendly's internal API serves event type data for the booking widget
        event_type_url = f"https://calendly.com/api/booking/event_types/{owner}/{event_slug}"
        try:
            resp = await client.get(event_type_url, headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            })
            if resp.status_code != 200:
                return {
                    "success": False,
                    "error": f"Calendly event type fetch failed ({resp.status_code})",
                    "url": url,
                }
            event_data = resp.json()
        except Exception as e:
            return {"success": False, "error": f"Calendly API error: {e}", "url": url}

        event_type = event_data.get("resource", event_data)
        event_uuid = event_type.get("uuid") or event_type.get("uri", "").split("/")[-1]
        duration = event_type.get("duration", 30)

        if not event_uuid:
            return {"success": False, "error": "Could not get Calendly event UUID", "url": url}

        # Step 2: Get available slots for next 2 weeks
        now = datetime.now(timezone.utc)
        range_start = now.strftime("%Y-%m-%d")
        range_end = (now + timedelta(days=14)).strftime("%Y-%m-%d")

        slots_url = (
            f"https://calendly.com/api/booking/event_types/{event_uuid}"
            f"/calendar/range?timezone={user_tz}"
            f"&diagnostics=false&range_start={range_start}&range_end={range_end}"
        )
        try:
            resp = await client.get(slots_url, headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0",
            })
            if resp.status_code != 200:
                return {
                    "success": False,
                    "error": f"Calendly slots fetch failed ({resp.status_code})",
                    "url": url,
                }
            slots_data = resp.json()
        except Exception as e:
            return {"success": False, "error": f"Calendly slots error: {e}", "url": url}

        # Parse available slots from Calendly's calendar response
        # Calendly returns: {"days": [{"date": "2024-01-15", "spots": [{"start_time": "...", ...}]}]}
        all_slots: list[dict[str, str]] = []
        for day in slots_data.get("days", []):
            for spot in day.get("spots", []):
                start_time = spot.get("start_time") or ""
                if start_time and spot.get("status") == "available":
                    all_slots.append({
                        "start": start_time,
                        "invitees_remaining": str(spot.get("invitees_remaining", 1)),
                    })

        if not all_slots:
            return {
                "success": False,
                "error": "No available slots found on Calendly in the next 2 weeks",
                "url": url,
            }

        # Step 3: Pick best slot
        chosen = _pick_best_slot(all_slots, user_tz)
        if not chosen:
            return {
                "success": False,
                "error": "No suitable business-hours slot found",
                "url": url,
            }

        chosen_time = chosen["start"]

        # Step 4: Book the slot
        first_name = _first_name(booker_name, "")
        last_name = " ".join(booker_name.split()[1:]) if len(booker_name.split()) > 1 else ""

        booking_payload = {
            "event": {
                "start_time": chosen_time,
                "location_configuration": {"location": None, "additional_info": ""},
            },
            "invitee": {
                "name": booker_name,
                "email": booker_email,
                "first_name": first_name,
                "last_name": last_name,
                "time_notation": "24h",
                "timezone": user_tz,
                "full_name": booker_name,
            },
            "event_type_uuid": event_uuid,
        }

        try:
            resp = await client.post(
                "https://calendly.com/api/booking/invitees",
                json=booking_payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
            )
            if resp.status_code in (200, 201):
                result = resp.json()
                return {
                    "success": True,
                    "provider": "calendly",
                    "booked_time": chosen_time,
                    "duration_minutes": duration,
                    "confirmation": result,
                }
            else:
                error_body = resp.text[:300]
                return {
                    "success": False,
                    "error": f"Calendly booking failed ({resp.status_code}): {error_body}",
                    "url": url,
                    "attempted_time": chosen_time,
                }
        except Exception as e:
            return {
                "success": False,
                "error": f"Calendly booking error: {e}",
                "url": url,
                "attempted_time": chosen_time,
            }


async def _book_calcom(
    url: str,
    booker_name: str,
    booker_email: str,
    user_tz: str = "UTC",
) -> dict[str, Any]:
    """Book a meeting on Cal.com using their public API.

    Flow:
    1. GET available slots from the public slots API
    2. Pick the best slot
    3. POST booking
    """
    username, event_slug = _parse_calcom_path(url)
    if not username:
        return {"success": False, "error": "Could not parse Cal.com URL"}

    # Default event slug if not provided (Cal.com defaults to the first event type)
    if not event_slug:
        event_slug = "30min"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        # Step 1: Get available slots
        now = datetime.now(timezone.utc)
        start_time = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        end_time = (now + timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        slots_url = (
            f"https://cal.com/api/slots"
            f"?eventTypeSlug={event_slug}"
            f"&usernameList={username}"
            f"&startTime={start_time}"
            f"&endTime={end_time}"
            f"&timeZone={user_tz}"
        )

        try:
            resp = await client.get(slots_url, headers={
                "Accept": "application/json",
            })
            if resp.status_code != 200:
                return {
                    "success": False,
                    "error": f"Cal.com slots fetch failed ({resp.status_code})",
                    "url": url,
                }
            slots_data = resp.json()
        except Exception as e:
            return {"success": False, "error": f"Cal.com slots error: {e}", "url": url}

        # Cal.com returns: {"slots": {"2024-01-15": [{"time": "2024-01-15T10:00:00.000Z"}]}}
        all_slots: list[dict[str, str]] = []
        slots_by_date = slots_data.get("slots", {})
        for date_key, day_slots in slots_by_date.items():
            for slot in day_slots:
                time_str = slot.get("time") or ""
                if time_str:
                    all_slots.append({"start": time_str, "time": time_str})

        if not all_slots:
            return {
                "success": False,
                "error": "No available slots found on Cal.com in the next 2 weeks",
                "url": url,
            }

        # Step 2: Pick best slot
        chosen = _pick_best_slot(all_slots, user_tz)
        if not chosen:
            return {
                "success": False,
                "error": "No suitable business-hours slot found",
                "url": url,
            }

        chosen_time = chosen.get("start") or chosen.get("time", "")

        # Step 3: Get event type ID (needed for booking)
        # Cal.com public API needs eventTypeId — fetch from event-types endpoint
        event_type_id = None
        try:
            et_resp = await client.get(
                f"https://cal.com/api/event-types"
                f"?usernameList={username}&eventTypeSlug={event_slug}",
                headers={"Accept": "application/json"},
            )
            if et_resp.status_code == 200:
                et_data = et_resp.json()
                # Response format varies — try common patterns
                if isinstance(et_data, list) and et_data:
                    event_type_id = et_data[0].get("id")
                elif isinstance(et_data, dict):
                    event_types = et_data.get("eventTypes", et_data.get("event_types", []))
                    if event_types:
                        event_type_id = event_types[0].get("id")
        except Exception as e:
            logger.debug("Cal.com event type ID fetch failed: %s", e)

        # Step 4: Book the slot
        # Calculate end time (assume 30-min event if no duration info)
        try:
            start_dt = datetime.fromisoformat(chosen_time.replace("Z", "+00:00"))
            end_dt = start_dt + timedelta(minutes=30)
            end_time_str = end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except (ValueError, TypeError):
            end_time_str = ""

        booking_payload: dict[str, Any] = {
            "start": chosen_time,
            "end": end_time_str,
            "name": booker_name,
            "email": booker_email,
            "timeZone": user_tz,
            "language": "en",
            "metadata": {},
            "responses": {
                "name": booker_name,
                "email": booker_email,
            },
        }
        if event_type_id:
            booking_payload["eventTypeId"] = event_type_id

        try:
            resp = await client.post(
                "https://cal.com/api/book/event",
                json=booking_payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            if resp.status_code in (200, 201):
                result = resp.json()
                return {
                    "success": True,
                    "provider": "calcom",
                    "booked_time": chosen_time,
                    "duration_minutes": 30,
                    "confirmation": result,
                }
            else:
                error_body = resp.text[:300]
                return {
                    "success": False,
                    "error": f"Cal.com booking failed ({resp.status_code}): {error_body}",
                    "url": url,
                    "attempted_time": chosen_time,
                }
        except Exception as e:
            return {
                "success": False,
                "error": f"Cal.com booking error: {e}",
                "url": url,
                "attempted_time": chosen_time,
            }


# ── Browser agent fallback for unsupported providers ──

_BROWSER_USE_AVAILABLE: bool | None = None
_BROWSER_AGENT_TIMEOUT = 120  # seconds

_ISO_DATETIME_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:[+-]\d{2}:\d{2}|Z)?"
)


def _check_browser_use() -> bool:
    """Check if browser-use is installed. Result is cached."""
    global _BROWSER_USE_AVAILABLE
    if _BROWSER_USE_AVAILABLE is None:
        try:
            import browser_use  # noqa: F401

            _BROWSER_USE_AVAILABLE = True
        except ImportError:
            _BROWSER_USE_AVAILABLE = False
    return _BROWSER_USE_AVAILABLE


def _get_browser_llm():  # type: ignore[return]
    """Build a LangChain chat model from HeyLead's configured API keys.

    browser-use accepts ChatGoogleGenerativeAI, ChatAnthropic, or ChatOpenAI.
    Returns None if no key is available.
    """
    from ..ai.llm import resolve_model
    from ..config import get_api_key, load_config
    from .. import constants

    cfg = load_config()
    priority = cfg.get("llm_priority", constants.DEFAULT_LLM_PRIORITY)

    for provider in priority:
        key = get_api_key(provider)
        if not key:
            continue
        # Driving a browser is reasoning-heavy — use the quality tier.
        model = resolve_model(provider, constants.LLM_TIER_QUALITY)
        try:
            if provider == "gemini":
                from langchain_google_genai import ChatGoogleGenerativeAI

                return ChatGoogleGenerativeAI(model=model, google_api_key=key)
            elif provider == "claude":
                from langchain_anthropic import ChatAnthropic

                return ChatAnthropic(model=model, api_key=key)
            elif provider == "openai":
                from langchain_openai import ChatOpenAI

                return ChatOpenAI(model=model, api_key=key)
        except Exception as e:
            logger.debug("Failed to create %s LLM for browser agent: %s", provider, e)
            continue
    return None


def _extract_datetime_from_text(text: str) -> str | None:
    """Extract the first ISO 8601 datetime from free text."""
    match = _ISO_DATETIME_RE.search(text)
    return match.group(0) if match else None


async def _book_browser_agent(
    url: str,
    booker_name: str,
    booker_email: str,
    user_tz: str = "UTC",
) -> dict[str, Any]:
    """Book a meeting using an AI browser agent for unsupported providers.

    Uses browser-use to:
    1. Navigate to the calendar URL
    2. Understand the booking UI visually
    3. Select a business-hours slot 2+ days out
    4. Fill in name and email
    5. Confirm the booking

    Requires: pip install heylead[browser] && playwright install chromium
    """
    from browser_use import Agent, Browser, BrowserConfig

    llm = _get_browser_llm()
    if llm is None:
        return {
            "success": False,
            "provider": "browser_agent",
            "error": "No LLM API key configured for browser agent",
            "url": url,
        }

    parsed = urlparse(url)
    provider_name = (parsed.hostname or "unknown").replace("www.", "").split(".")[0]

    task = (
        f"Go to {url} and book a meeting with these details:\n"
        f"- Name: {booker_name}\n"
        f"- Email: {booker_email}\n"
        f"- Timezone: {user_tz}\n\n"
        f"Slot selection rules:\n"
        f"- Pick a slot at least 2 days from now\n"
        f"- Prefer weekday business hours (9 AM - 5 PM)\n"
        f"- Prefer mid-morning (10-11 AM) or early afternoon (2-3 PM)\n"
        f"- If asked for a meeting topic, say 'Quick sync'\n\n"
        f"After booking, report the confirmed date and time in ISO 8601 format.\n"
        f"If you cannot complete the booking, explain why."
    )

    browser = Browser(config=BrowserConfig(headless=True))
    agent = Agent(task=task, llm=llm, browser=browser)

    try:
        result = await asyncio.wait_for(
            agent.run(),
            timeout=_BROWSER_AGENT_TIMEOUT,
        )

        final_text = str(result)
        booked_time = _extract_datetime_from_text(final_text)

        if booked_time and "error" not in final_text.lower() and "could not" not in final_text.lower():
            return {
                "success": True,
                "provider": provider_name,
                "booked_time": booked_time,
                "duration_minutes": 30,
                "confirmation": {"agent_response": final_text[:500]},
            }
        else:
            return {
                "success": False,
                "provider": provider_name,
                "error": f"Browser agent could not complete booking: {final_text[:300]}",
                "url": url,
            }
    except asyncio.TimeoutError:
        return {
            "success": False,
            "provider": provider_name,
            "error": f"Browser agent timed out after {_BROWSER_AGENT_TIMEOUT}s",
            "url": url,
        }
    except Exception as e:
        logger.warning("Browser agent booking failed: %s", e)
        return {
            "success": False,
            "provider": provider_name,
            "error": f"Browser agent error: {e}",
            "url": url,
        }
    finally:
        try:
            await browser.close()
        except Exception:
            pass


async def book_meeting(
    calendar_url: str,
    booker_name: str,
    booker_email: str,
    user_tz: str = "UTC",
) -> dict[str, Any]:
    """Book a meeting on a prospect's calendar.

    Detects the provider and routes to the appropriate booking flow.

    Args:
        calendar_url: The prospect's calendar/booking URL
        booker_name: Name to book with (the HeyLead user)
        booker_email: Email to book with (the HeyLead user)
        user_tz: Timezone for slot selection (e.g., "Europe/Kyiv")

    Returns:
        Dict with:
        - success: bool
        - provider: str (calendly, calcom, unknown)
        - booked_time: str (ISO datetime if booked)
        - duration_minutes: int
        - error: str (if failed)
        - url: str (original URL for manual fallback)
    """
    provider = detect_provider(calendar_url)
    logger.info("Booking meeting on %s: %s", provider, calendar_url)

    if provider == "calendly":
        return await _book_calendly(calendar_url, booker_name, booker_email, user_tz)
    elif provider == "calcom":
        return await _book_calcom(calendar_url, booker_name, booker_email, user_tz)
    else:
        # Try AI browser agent for unknown providers
        if _check_browser_use():
            logger.info("Attempting browser agent booking for: %s", calendar_url)
            result = await _book_browser_agent(
                calendar_url, booker_name, booker_email, user_tz
            )
            if result.get("success"):
                return result
            logger.info(
                "Browser agent failed, falling back to manual: %s",
                result.get("error"),
            )

        return {
            "success": False,
            "provider": "unknown",
            "error": f"Unsupported calendar provider. Book manually: {calendar_url}",
            "url": calendar_url,
        }


def format_booking_result(result: dict[str, Any]) -> str:
    """Format a booking result for display to the user."""
    if result.get("success"):
        booked = result.get("booked_time", "")
        provider = result.get("provider", "")
        duration = result.get("duration_minutes", 30)
        try:
            dt = datetime.fromisoformat(booked.replace("Z", "+00:00"))
            time_str = dt.strftime("%A, %B %d at %I:%M %p %Z")
        except (ValueError, TypeError):
            time_str = booked
        return (
            f"Meeting booked on {provider.title()}!\n"
            f"   When: {time_str} ({duration} min)\n"
            f"   Confirmation sent to your email."
        )
    else:
        error = result.get("error", "Unknown error")
        url = result.get("url", "")
        attempted = result.get("attempted_time", "")
        msg = f"Could not auto-book: {error}"
        if url:
            msg += f"\n   Book manually: {url}"
        if attempted:
            msg += f"\n   Attempted slot: {attempted}"
        return msg
