"""The gate in front of every LLM call that sees a resume (rules L1-L3, L8).

Fail closed: an LLM call is allowed only if **all** hold, checked in this order,
and any miss means the rules alone do the import.

1. ``PROFILE_IMPORT_LLM_ALLOWED_PROVIDERS`` is non-empty (an empty list is the
   default: no provider is trusted with resume text until an operator lists one
   that they know does not retain it);
2. the configured ``PROFILE_IMPORT_LLM_PROVIDER`` is in that list and is a known
   provider;
3. that provider's API key is set;
4. the user has consented, at the **current** consent version;
5. the user is under the daily cap.

The check runs inside the Celery task, immediately before the call, on a freshly
read profile, so consent withdrawn while a job waited is honoured.
"""
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.llm_providers import PROVIDER_CONFIGS


def _allowed_providers():
    return [p.strip() for p in (settings.PROFILE_IMPORT_LLM_ALLOWED_PROVIDERS or []) if p and p.strip()]


def provider_available():
    """Whether an operator has enabled AI-assisted import at all (items 1-3).
    Used to decide whether to show the consent control; says nothing about the
    user."""
    allowed = _allowed_providers()
    provider = settings.PROFILE_IMPORT_LLM_PROVIDER
    config = PROVIDER_CONFIGS.get(provider)
    return bool(allowed and provider in allowed and config and getattr(settings, config.api_key_setting, ""))


def _daily_key(user_id):
    return f"profile_import:llm:{user_id}:{timezone.localdate().isoformat()}"


def llm_import_allowed(profile, *, reserve=False):
    """``(allowed, reason)``. With ``reserve=True`` a successful check also
    counts against the user's daily cap (done once, right before the call)."""
    allowed = _allowed_providers()
    if not allowed:
        return False, "no_allowlist"
    provider = settings.PROFILE_IMPORT_LLM_PROVIDER
    if provider not in allowed:
        return False, "provider_not_allowed"
    config = PROVIDER_CONFIGS.get(provider)
    if config is None:
        return False, "unknown_provider"
    if not getattr(settings, config.api_key_setting, ""):
        return False, "no_api_key"
    if profile.llm_import_consent_at is None:
        return False, "no_consent"
    if profile.llm_import_consent_version != settings.PROFILE_IMPORT_CONSENT_VERSION:
        return False, "consent_outdated"
    key = _daily_key(profile.user_id)
    used = cache.get(key, 0)
    if used >= settings.PROFILE_IMPORT_LLM_DAILY_CAP:
        return False, "daily_cap"
    if reserve:
        cache.add(key, 0, 24 * 3600)
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, 24 * 3600)
    return True, ""
