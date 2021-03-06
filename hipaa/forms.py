"""
This monkey patches a bunch of the django.contrib.auth forms
"""
import gzip
import os
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm
from django.contrib.auth.hashers import check_password
from django.core.exceptions import ImproperlyConfigured
from django.forms import ValidationError
from django.utils.timezone import now

from .models import Log

# allow a user to try to login n times per unit of time
LOGIN_RATE_LIMIT = getattr(settings, "LOGIN_RATE_LIMIT", (20, timedelta(minutes=10)))

CANNOT_USE_LAST_N_PASSWORDS = getattr(settings, "CANNOT_USE_LAST_N_PASSWORDS", 24)


def get_logger():
    """
    Returns the (first) Log class that subclasses this package's Log model
    """
    try:
        Logger = list(Log.__subclasses__())[0]
    except IndexError:
        raise ImproperlyConfigured("You must have a model subclass of 'hipaa.Log'")

    return Logger


# Monkey patches the AuthenticationForm.clean method so it takes into account the
# LOGIN_RATE_LIMIT
def authentication_form_clean(self, clean=AuthenticationForm.clean):
    """
    This adds rate limiting to the login form
    """
    Logger = get_logger()
    # if there was no username, no need to consider a ratelimit
    if self.cleaned_data.get("username"):
        # we key the rate limit based on the username on the form entered, and the
        # IP address
        log_info = Logger.info(
            request=self.request,
            action=Log.PASSWORD_ATTEMPT,
            extra=self.cleaned_data.get("username")
        )

        # this is the date to go back in the Log records to find out how many
        # logins have been performed in the set amount of time
        offset_date = now() - LOGIN_RATE_LIMIT[1]

        # figure out when the last password reset action was performed, so we
        # can use *that* as the offset date instead (so after you reset your
        # password, you can always log back in). This won't work if someone is
        # purposely trying to DOS you (but we won't handle that case)
        UserModel = get_user_model()
        user = UserModel.objects.filter(**{self.username_field.name: self.cleaned_data.get("username")}).first()
        last_reset = Logger.objects.filter(action=Log.PASSWORD_RESET, user=user).order_by("-pk").exclude(user=None).first()
        if last_reset:
            offset_date = max(last_reset.created_on, offset_date)

        # if there were too many attempts, raise a validation error
        if Logger.objects.filter(
                ip_address=log_info.ip_address,
                action=Log.PASSWORD_ATTEMPT,
                extra=self.cleaned_data.get("username"),
                created_on__gte=offset_date).count() > LOGIN_RATE_LIMIT[0]:
            raise ValidationError(
                "Too many login attempts. You can reset your password to login again or wait %d seconds"
                % LOGIN_RATE_LIMIT[1].total_seconds()
            )

    return clean(self)

AuthenticationForm.clean = authentication_form_clean


# hook into the SetPasswordForm (which is used to reset a password), so we can
# log it happened
def log_password_change(self, save=SetPasswordForm.save):
    """
    This adds logging to the SetPasswordForm which is used when a password is
    being reset
    """
    Logger = get_logger()
    save(self)
    # we log this as a PasswordReset (even though it could also just be a password change).
    # We keep track of the hashed password, so when they try to change their
    # password again, we can make sure they are not reusing an old password
    Logger.info(action=Logger.PASSWORD_RESET, user=self.user, extra=self.user.password)

SetPasswordForm.save = log_password_change


# hook into SetPasswordForm again so we can ensure the password meets certain
# requirements

password_list_path = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), 'common-passwords.txt.gz'
)


def ensure_safe_password(self, clean_new_password2=SetPasswordForm.clean_new_password2):
    password2 = clean_new_password2(self)
    if password2:
        if len(password2) < 8:
            raise ValidationError("The password must be 8 characters or longer")

        if not re.search("[0-9]", password2):
            raise ValidationError("The password must have at least one number")

        if not re.search("[A-Za-z]", password2):
            raise ValidationError("The password must have at least one letter")

        if getattr(self.user, self.user.USERNAME_FIELD) in password2 or (self.user.email and self.user.email in password2):
            raise ValidationError("The password must not contain your username/email")

        if (self.user.first_name and self.user.first_name in password2) or (self.user.last_name and self.user.last_name in password2):
            raise ValidationError("The password must not contain your name")

        common_passwords_lines = gzip.open(password_list_path).read().decode('utf-8').splitlines()
        if password2 in common_passwords_lines:
            raise ValidationError("The password is too common.")

        # make sure they haven't used this password before
        Logger = get_logger()
        previous_password_hashes = Logger.objects.filter(
            user=self.user,
            action=Log.PASSWORD_RESET
        ).order_by("-pk").exclude(extra="").values_list("extra", flat=True)[:CANNOT_USE_LAST_N_PASSWORDS]
        for previous_hash in previous_password_hashes:
            if check_password(password2, previous_hash):
                raise ValidationError("The password has been used by you before", code="password-reuse")


SetPasswordForm.clean_new_password2 = ensure_safe_password
