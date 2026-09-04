from django.conf import settings
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import translation
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .models import UserLanguagePreference


SUPPORTED_LANGUAGES = {code for code, _label in settings.LANGUAGES}


def _set_language_cookie(response, language):
    response.set_cookie(
        settings.LANGUAGE_COOKIE_NAME,
        language,
        max_age=getattr(settings, "LANGUAGE_COOKIE_AGE", None),
        path=getattr(settings, "LANGUAGE_COOKIE_PATH", "/"),
        domain=getattr(settings, "LANGUAGE_COOKIE_DOMAIN", None),
        secure=getattr(settings, "LANGUAGE_COOKIE_SECURE", False),
        httponly=getattr(settings, "LANGUAGE_COOKIE_HTTPONLY", False),
        samesite=getattr(settings, "LANGUAGE_COOKIE_SAMESITE", None),
    )


@require_POST
def set_application_language(request):
    language = request.POST.get("language", "")
    next_url = request.POST.get("next") or reverse("dashboard")
    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse("dashboard")

    response = HttpResponseRedirect(next_url)
    if language not in SUPPORTED_LANGUAGES or not translation.check_for_language(language):
        return response

    translation.activate(language)
    _set_language_cookie(response, language)
    if request.user.is_authenticated:
        UserLanguagePreference.objects.update_or_create(
            user=request.user,
            defaults={"language": language},
        )
    return response


class UserLanguagePreferenceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        preferred_language = None
        user = getattr(request, "user", None)
        if user and user.is_authenticated:
            preferred_language = UserLanguagePreference.objects.filter(
                user_id=user.pk
            ).values_list("language", flat=True).first()
            if preferred_language in SUPPORTED_LANGUAGES:
                translation.activate(preferred_language)
                request.LANGUAGE_CODE = preferred_language

        response = self.get_response(request)
        if preferred_language and request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME) != preferred_language:
            _set_language_cookie(response, preferred_language)
        return response
