# Copyright (c) 2014, Fortylines LLC
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   * Redistributions of source code must retain the above copyright notice,
#     this list of conditions and the following disclaimer.
#   * Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""Extra Forms and Views that might prove useful to register users."""

from django import forms
from django.core.urlresolvers import reverse, reverse_lazy
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth import authenticate
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.shortcuts import render, redirect
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _
from django.views.generic.base import TemplateView
from django.views.generic.edit import FormView
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.cache import never_cache

from signup.decorators import check_user_active
from signup import signals
from signup import settings

UserModel = get_user_model()

def _redirect_to(url):
    try:
        to_url, args, kwargs = url
        return redirect(to_url, *args, **kwargs)
    except ValueError:
        return redirect(url)


class NameEmailForm(forms.Form):
    """
    Form for frictionless registration of a new account. Just supply
    a full name and an email and you are in. We will ask for username
    and password later.
    """
    full_name = forms.RegexField(
        regex=r'^[\w\s]+$', max_length=60,
        widget=forms.TextInput(attrs={'placeholder':'Full Name'}),
        label=_("Full Name"),
        error_messages={'invalid':
            _("Sorry we do not recognize some characters in your full name.")})
    email = forms.EmailField(
        widget=forms.TextInput(attrs={'placeholder':'Email',
                                      'maxlength': 75}),
        label=_("E-mail"))


class SignupView(FormView):
    """
    A frictionless registration backend With a full name and email
    address, the user is immediately signed up and logged in.
    """

    form_class = NameEmailForm
    template_name = 'registration/registration_form.html'
    success_url = settings.LOGIN_REDIRECT_URL
    fail_url = ('registration_register', (), {})

    def post(self, request, *args, **kwargs):
        # Pass request to get_form_class and get_form for per-request
        # form control.
        form_class = self.get_form_class()
        form = self.get_form(form_class)
        if form.is_valid():
            # Pass request to form_valid.
            return self.form_valid(form, request)
        else:
            return self.form_invalid(form, request)

    def form_invalid(self, form, request):
        return self.render_to_response(self.get_context_data(form=form))

    def form_valid(self, form, request):
        new_user = self.register(request, **form.cleaned_data)
        if new_user:
            success_url = self.get_success_url(request)
        else:
            success_url = request.META['PATH_INFO']
        return _redirect_to(success_url)

    def get_context_data(self, **kwargs):
        context = super(SignupView, self).get_context_data(**kwargs)
        next_url = self.request.GET.get(REDIRECT_FIELD_NAME, None)
        if next_url:
            context.update({REDIRECT_FIELD_NAME: next_url })
        return context

    def get_success_url(self, request=None):
        next_url = request.GET.get(REDIRECT_FIELD_NAME, None)
        if not next_url:
            next_url = super(SignupView, self).get_success_url()
        return next_url

    def register(self, request, **cleaned_data):
        full_name, email = cleaned_data['full_name'], cleaned_data['email']
        users = UserModel.objects.filter(email=email)
        if users.exists():
            user = users.get()
            if check_user_active(request, user,
                                 next_url=self.get_success_url(request)):
                messages.info(request, mark_safe(_(
                    'This email address has already been registered!'\
' Please <a href="%s">login</a> with your credentials. Thank you.'
                    % reverse('login'))))
            else:
                messages.info(request, mark_safe(_(
                    "This email address has already been registered!"\
" You should now secure and activate your account following "\
" the instructions we just emailed you. Thank you.")))
            return None

        name_parts = full_name.split(' ')
        if len(name_parts) > 0:
            first_name = name_parts[0]
            last_name = ' '.join(name_parts[1:])
        else:
            first_name = full_name
            last_name = ''
        username = None
        if cleaned_data.has_key('username'):
            username = cleaned_data['username']
        if username and cleaned_data.has_key('password'):
            user = UserModel.objects.create_user(
                username=username, password=cleaned_data['password'],
                email=email, first_name=first_name, last_name=last_name)
        else:
            user = UserModel.objects.create_inactive_user(
                username=username,
                email=email, first_name=first_name, last_name=last_name)
        signals.user_registered.send(
            sender=__name__, user=user, request=request)

        # Bypassing authentication here, we are doing frictionless registration
        # the first time around.
        user.backend = 'django.contrib.auth.backends.ModelBackend'
        auth_login(request, user)
        return user


class ActivationView(TemplateView):
    """
    The user is now on the activation url that was sent in an email.
    It is time to activate the account.
    """
    http_method_names = ['get']
    template_name = 'registration/activate.html'
    token_generator = default_token_generator

    def get(self, request, *args, **kwargs):
        verification_key = kwargs['verification_key']
        user = UserModel.objects.find_user(verification_key)
        if user:
            if user.has_invalid_password:
                messages.info(self.request,
                    _("Please set a password to protect your account."))
                url = reverse('registration_password_confirm',
                              args=(user.email_verification_key,
                                    self.token_generator.make_token(user)))
            else:
                user = UserModel.objects.activate_user(verification_key)
                signals.user_activated.send(
                    sender=__name__, user=user, request=request)
                messages.info(self.request,
                    _("Thank you. Your account is now active." \
                          " You can sign in at your convienience."))
                url = reverse('login')
            next_url = request.GET.get(REDIRECT_FIELD_NAME, None)
            if next_url:
                success_url = "%s?%s=%s" % (url, REDIRECT_FIELD_NAME, next_url)
            else:
                success_url = url
            return _redirect_to(success_url)
        return super(ActivationView, self).get(request, *args, **kwargs)


@sensitive_post_parameters()
@never_cache
def registration_password_confirm(request, verification_key, token=None,
        template_name='registration/password_reset_confirm.html',
        token_generator=default_token_generator,
        set_password_form=SetPasswordForm,
        post_reset_redirect=reverse_lazy('accounts_profile'),
        extra_context=None,
        redirect_field_name=REDIRECT_FIELD_NAME):
    """
    View that checks the hash in a password activation link and presents a
    form for entering a new password. We can activate the account for real
    once we know the email is valid and a password has been set.
    """
    redirect_to = request.REQUEST.get(redirect_field_name, None)

    # We use *find_user* instead of *activate_user* because we want
    # to make sure both the *verification_key* and *token* are valid
    # before doing any modification of the underlying models.
    user = UserModel.objects.find_user(verification_key)
    if user is not None and token_generator.check_token(user, token):
        validlink = True
        if request.method == 'POST':
            form = set_password_form(user, request.POST)
            if form.is_valid():
                form.save()
                user = UserModel.objects.activate_user(verification_key)
                signals.user_activated.send(
                    sender=__name__, user=user, request=request)
                messages.info(request,
                    _("Thank you. Your account is now active."))

                # Okay, security check complete. Log the user in.
                user_with_backend = authenticate(
                    username=user.username,
                    password=form.cleaned_data.get('new_password1'))
                auth_login(request, user_with_backend)
                if request.session.test_cookie_worked():
                    request.session.delete_test_cookie()

                if redirect_to is None:
                    redirect_to = reverse('accounts_profile')
                return redirect(redirect_to)
        else:
            form = set_password_form(None)
    else:
        validlink = False
        form = None
    context = {
        'form': form,
        'validlink': validlink,
    }
    if redirect_to:
        context.update({redirect_field_name: redirect_to})
    if extra_context is not None:
        context.update(extra_context)
    return render(request, template_name, context)


@login_required
def redirect_to_user_profile(request):
    return redirect(request.user.get_absolute_url())
