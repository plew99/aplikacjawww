import datetime
import hashlib
import json
import mimetypes
import os
import sys
from typing import Dict, Any, Optional
from urllib.parse import urljoin

import bleach
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.models import User
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import SuspiciousOperation
from django.db import OperationalError, ProgrammingError
from django.db.models import Q, QuerySet
from django.db.models.query import Prefetch
from django.http import JsonResponse, HttpResponse, HttpRequest, HttpResponseForbidden
from django.http.response import HttpResponseBadRequest, HttpResponseNotFound
from django.shortcuts import render, redirect, get_object_or_404
from django.template import Template, Context
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_bleach.utils import get_bleach_default_options
from django_sendfile import sendfile

from wwwforms.models import Form, FormQuestionAnswer, FormQuestion
from .forms import ArticleForm, UserProfileForm, UserForm, \
    UserProfilePageForm, UserSecretNotesForm, WorkshopForm, UserCoverLetterForm, WorkshopParticipantPointsForm, \
    TinyMCEUpload, SolutionFileFormSet, SolutionForm, CampInterestEmailForm
from .models import Article, UserProfile, Workshop, WorkshopParticipant, \
    CampParticipant, ResourceYearPermission, Camp, Solution, CampInterestEmail
from .templatetags.wwwtags import qualified_mark


def get_context(request):
    context = {}

    if request.user.is_authenticated:
        visible_resources = ResourceYearPermission.objects.exclude(access_url__exact="")
        if request.user.has_perm('wwwapp.access_all_resources'):
            context['resources'] = visible_resources
        else:
            try:
                user_profile = UserProfile.objects.get(user=request.user)
                context['resources'] = visible_resources.filter(year__in=user_profile.all_participation_years())
            except UserProfile.DoesNotExist:
                context['resources'] = []

    context['google_analytics_key'] = settings.GOOGLE_ANALYTICS_KEY
    context['articles_on_menubar'] = Article.objects.filter(on_menubar=True).all()
    context['years'] = Camp.objects.all()
    context['current_year'] = Camp.current()

    return context


def redirect_to_view_for_latest_year(target_view_name):
    def view(request):
        url = reverse(target_view_name, args=[Camp.current().pk])
        args = request.META.get('QUERY_STRING', '')
        if args:
            url = "%s?%s" % (url, args)
        return redirect(url)
    return view


def program_view(request, year):
    year = get_object_or_404(Camp, pk=year)

    context = {}
    context['title'] = 'Program %s' % str(year)

    camp_participation = request.user.user_profile.camp_participation_for(year) if request.user.is_authenticated else None
    if camp_participation:
        workshop_participation = camp_participation.workshop_participation.all()
        workshops_participating_in = set(wp.workshop for wp in workshop_participation)
        has_results = any(wp.qualification_result is not None for wp in workshop_participation)
    else:
        workshops_participating_in = set()
        has_results = False

    workshops = year.workshops.filter(Q(status='Z') | Q(status='X')).order_by('title').prefetch_related('lecturer', 'lecturer__user', 'type', 'category')
    context['workshops'] = [(workshop, (workshop in workshops_participating_in)) for workshop
                            in workshops]
    context['has_results'] = has_results and year == Camp.current()
    context['is_registered'] = camp_participation is not None
    if year.is_qualification_editable():
        camp_interest_email_form = CampInterestEmailForm(user=request.user, is_registered=camp_participation is not None)
        camp_interest_email_form.helper.form_action = reverse('register_to_camp', args=[year.pk])
        context['camp_interest_email_form'] = camp_interest_email_form

    context['selected_year'] = year
    return render(request, 'program.html', context)


def register_to_camp_view(request, year):
    year = get_object_or_404(Camp, pk=year)

    if not year.is_qualification_editable():
        return HttpResponseForbidden('Kwalifikacja na te warsztaty została zakończona.')

    form = CampInterestEmailForm(request.POST, user=request.user)
    if form.is_valid():
        created = None
        if request.user.is_authenticated:
            # Logged in user flow
            _, created = year.participants.get_or_create(user_profile=request.user.user_profile)
        else:
            # Email flow
            email = form.cleaned_data['email']
            user = User.objects.filter(email=email).order_by('-last_login').first()
            if user:
                # We have a user with this email
                _, created = year.participants.get_or_create(user_profile=user.user_profile)
            else:
                # We have no user with this email - add an unregistered user entry
                _, created = year.interested_via_email.get_or_create(email=email)
        if created:
            messages.info(request, 'Powiadomimy Cię, gdy rozpocznie się rejestracja', extra_tags='auto-dismiss')
        else:
            messages.warning(request, 'Już znajdujesz się już na tegorocznej liście', extra_tags='auto-dismiss')
    else:
        messages.error(request, form.errors.as_ul(), extra_tags='auto-dismiss danger')  # HACK: it should be alert-danger instead of alert-error
    return redirect('program', year.pk)


def profile_view(request, user_id):
    """
    This function allows to view other people's profile by id.
    However, to view them easily some kind of resolver might be needed as we don't have usernames.
    """
    context = {}
    user_id = int(user_id)
    user = get_object_or_404(User.objects.prefetch_related(
        'user_profile',
        'user_profile__user',
        'user_profile__camp_participation',
        'user_profile__camp_participation__year',
        'user_profile__camp_participation__workshop_participation',
        'user_profile__camp_participation__workshop_participation__workshop',
        'user_profile__camp_participation__workshop_participation__workshop__year',
        'user_profile__camp_participation__workshop_participation__solution',
        'user_profile__lecturer_workshops',
        'user_profile__lecturer_workshops__year',
    ), pk=user_id)

    camp_participant = user.user_profile.camp_participation_for(year=Camp.current())

    is_my_profile = (request.user == user)
    can_see_all_users = request.user.has_perm('wwwapp.see_all_users')
    can_see_all_workshops = request.user.has_perm('wwwapp.see_all_workshops')
    can_use_secret_notes = request.user.has_perm('wwwapp.use_secret_notes')

    can_qualify = request.user.has_perm('wwwapp.change_campparticipant')
    context['can_qualify'] = can_qualify
    context['can_see_all_workshops'] = can_see_all_workshops
    context['can_use_secret_notes'] = can_use_secret_notes
    context['camp_participation'] = camp_participant

    if can_use_secret_notes:
        context['secret_notes_form'] = UserSecretNotesForm(instance=user.user_profile)

    if request.method == 'POST':
        if not request.user.is_authenticated:
            return redirect_to_login(reverse('profile', args=[user_id]))
        if 'qualify' in request.POST:
            if not can_qualify:
                return HttpResponseForbidden()
            if not camp_participant:
                return HttpResponseNotFound('This user is not registered for the current edition')
            if request.POST['qualify'] == 'accept':
                camp_participant.status = CampParticipant.STATUS_ACCEPTED
                camp_participant.save()
            elif request.POST['qualify'] == 'reject':
                camp_participant.status = CampParticipant.STATUS_REJECTED
                camp_participant.save()
            elif request.POST['qualify'] == 'cancel':
                camp_participant.status = CampParticipant.STATUS_CANCELLED
                camp_participant.save()
            elif request.POST['qualify'] == 'delete':
                camp_participant.status = None
                camp_participant.save()
            else:
                raise SuspiciousOperation("Invalid argument")
        elif can_use_secret_notes and 'secret_note' in request.POST:
            context['secret_notes_form'] = UserSecretNotesForm(request.POST, instance=user.user_profile)
            if context['secret_notes_form'].is_valid():
                context['secret_notes_form'].save()
                messages.info(request, 'Zapisano.', extra_tags='auto-dismiss')
        else:
            raise SuspiciousOperation("Invalid request")
        return redirect('profile', user.pk)

    context['title'] = "{0.first_name} {0.last_name}".format(user)
    context['profile_page'] = user.user_profile.profile_page
    context['is_my_profile'] = is_my_profile
    context['gender'] = user.user_profile.gender

    if can_see_all_users or is_my_profile:
        context['profile'] = user.user_profile
        context['participation_data'] = user.user_profile.all_participation_data()
        if not can_see_all_workshops and not is_my_profile:
            # If the current user can't see non-public workshops, remove them from the list
            for participation in context['participation_data']:
                participation['workshops'] = [w for w in participation['workshops'] if w.is_publicly_visible()]

    if can_see_all_workshops:
        context['results_data'] = user.user_profile.workshop_results_by_year()

    if can_see_all_workshops or is_my_profile:
        context['lecturer_workshops'] = user.user_profile.lecturer_workshops.prefetch_related('year').all().order_by('-year')
    else:
        context['lecturer_workshops'] = user.user_profile.lecturer_workshops.prefetch_related('year').filter(Q(status='Z') | Q(status='X')).order_by('-year')

    return render(request, 'profile.html', context)


@login_required()
def mydata_profile_view(request):
    context = {}

    if request.method == "POST":
        user_form = UserForm(request.POST, instance=request.user)
        user_profile_form = UserProfileForm(request.POST, instance=request.user.user_profile)
        if user_form.is_valid() and user_profile_form.is_valid():
            user_form.save()
            user_profile_form.save()
            messages.info(request, 'Zapisano.', extra_tags='auto-dismiss')
            return redirect('mydata_profile')
    else:
        user_form = UserForm(instance=request.user)
        user_profile_form = UserProfileForm(instance=request.user.user_profile)

    context['user_form'] = user_form
    context['user_profile_form'] = user_profile_form
    context['title'] = 'Mój profil'

    return render(request, 'mydata_profile.html', context)


@login_required()
def mydata_profile_page_view(request):
    context = {}

    if request.method == "POST":
        user_profile_page_form = UserProfilePageForm(request.POST, instance=request.user.user_profile)
        if user_profile_page_form.is_valid():
            user_profile_page_form.save()
            messages.info(request, 'Zapisano.', extra_tags='auto-dismiss')
            return redirect('mydata_profile_page')
    else:
        user_profile_page_form = UserProfilePageForm(instance=request.user.user_profile)

    context['user_profile_page_form'] = user_profile_page_form
    context['title'] = 'Mój profil'

    return render(request, 'mydata_profilepage.html', context)


@login_required()
def mydata_cover_letter_view(request):
    context = {}

    user_profile = request.user.user_profile
    camp_participation = user_profile.camp_participation_for(Camp.current())

    if camp_participation is not None:
        if request.method == "POST":
            user_cover_letter_form = UserCoverLetterForm(request.POST, instance=camp_participation)
            if user_cover_letter_form.is_valid():
                user_cover_letter_form.save()
                messages.info(request, 'Zapisano.', extra_tags='auto-dismiss')
                return redirect('mydata_cover_letter')
        else:
            user_cover_letter_form = UserCoverLetterForm(instance=camp_participation)
    else:
        user_cover_letter_form = None

    context['user_cover_letter_form'] = user_cover_letter_form
    context['title'] = 'Mój profil'

    return render(request, 'mydata_coverletter.html', context)


@login_required()
def mydata_status_view(request):
    context = {}
    user_profile = UserProfile.objects.prefetch_related(
        'camp_participation',
        'camp_participation__year',
        'camp_participation__workshop_participation',
        'camp_participation__workshop_participation__workshop',
        'camp_participation__workshop_participation__workshop__year',
        'camp_participation__workshop_participation__solution',
        'lecturer_workshops',
        'lecturer_workshops__year',
    ).get(user=request.user)
    current_year = Camp.current()

    participation_data = user_profile.workshop_results_by_year()
    current_status = next(filter(lambda x: x['year'] == current_year, participation_data), None)
    past_status = list(filter(lambda x: x['year'] != current_year, participation_data))
    current_camp_participant = current_status['camp_participant'] if current_status else None

    context['title'] = 'Mój profil'
    context['gender'] = user_profile.gender
    context['has_completed_profile'] = user_profile.is_completed
    context['has_cover_letter'] = len(current_camp_participant.cover_letter) >= 50 if current_camp_participant else None
    context['current_status'] = current_status
    context['past_status'] = past_status

    return render(request, 'mydata_status.html', context)


@login_required()
def mydata_forms_view(request):
    context = {}

    context['user_info_forms'] = Form.visible_objects.all()
    context['title'] = 'Mój profil'

    return render(request, 'mydata_forms.html', context)


def can_edit_workshop(workshop, user):
    """
    Determines whether the user can see the edit views
    (but he may not be able to actually edit if if this is a workshop from a past edition - he can only see read-only
    state in that case)
    """
    if user.is_authenticated:
        is_lecturer = workshop.lecturer.filter(user=user).exists()
        has_perm_to_edit = is_lecturer or user.has_perm('wwwapp.edit_all_workshops')
        return has_perm_to_edit, is_lecturer
    else:
        return False, False


def workshop_page_view(request, year, name):
    workshop = get_object_or_404(Workshop, year=year, name=name)
    has_perm_to_edit, is_lecturer = can_edit_workshop(workshop, request.user)

    if not workshop.is_publicly_visible():  # Accepted or cancelled
        return HttpResponseForbidden("Warsztaty nie zostały zaakceptowane")

    if request.user.is_authenticated:
        registered = workshop.participants.filter(camp_participation__user_profile__user=request.user).exists()
    else:
        registered = False

    context = {}
    context['title'] = workshop.title
    context['workshop'] = workshop
    context['registered'] = registered
    context['is_lecturer'] = is_lecturer
    context['has_perm_to_edit'] = has_perm_to_edit
    context['has_perm_to_view_details'] = \
        has_perm_to_edit or request.user.has_perm('wwwapp.see_all_workshops')

    return render(request, 'workshoppage.html', context)


@login_required()
def workshop_edit_view(request, year, name=None):
    if name is None:
        year = get_object_or_404(Camp, pk=year)
        workshop = None
        title = 'Nowe warsztaty'
        has_perm_to_edit, is_lecturer = not year.is_program_finalized(), True
    else:
        workshop = get_object_or_404(Workshop, year=year, name=name)
        year = workshop.year
        title = workshop.title
        has_perm_to_edit, is_lecturer = can_edit_workshop(workshop, request.user)

    has_perm_to_see_all = request.user.has_perm('wwwapp.see_all_workshops')
    if workshop and not has_perm_to_edit and not has_perm_to_see_all:
        return HttpResponseForbidden()

    if workshop and request.method == 'POST' and 'qualify' in request.POST:
        if not request.user.has_perm('wwwapp.change_workshop_status') or not workshop.is_workshop_editable():
            return HttpResponseForbidden()
        if request.POST['qualify'] == 'accept':
            if workshop.year.is_program_finalized() and workshop.status != Workshop.STATUS_CANCELLED:
                return HttpResponseForbidden()
            workshop.status = Workshop.STATUS_ACCEPTED
            workshop.save()
        elif request.POST['qualify'] == 'reject':
            if workshop.year.is_program_finalized():
                return HttpResponseForbidden()
            workshop.status = Workshop.STATUS_REJECTED
            workshop.save()
        elif request.POST['qualify'] == 'cancel':
            workshop.status = Workshop.STATUS_CANCELLED
            workshop.save()
        elif request.POST['qualify'] == 'delete':
            if workshop.year.is_program_finalized():
                return HttpResponseForbidden()
            workshop.status = None
            workshop.save()
        else:
            raise SuspiciousOperation("Invalid argument")
        return redirect('workshop_edit', workshop.year.pk, workshop.name)

    # Generate the parts of the workshop URL displayed in the workshop slug editor
    workshop_url = request.build_absolute_uri(
        reverse('workshop_page', kwargs={'year': 9999, 'name': 'SOMENAME'}))
    workshop_url = workshop_url.split('SOMENAME')
    workshop_url[0:1] = workshop_url[0].split('9999')

    profile_warnings = []
    if is_lecturer:  # The user is one of the lecturers for this workshop
        if len(request.user.user_profile.profile_page) <= 50:  # The user does not have their profile page filled in
            profile_warnings.append(Template("""
                    <strong>Nie uzupełnił{% if user.user_profile.gender == 'F' %}aś{% else %}eś{% endif %} swojej
                    <a target="_blank" href="{% url 'mydata_profile_page' %}">strony profilowej</a>.</strong>
                    Powiedz potencjalnym uczestnikom coś więcej o sobie!
                """).render(Context({'user': request.user})))

    if workshop or has_perm_to_edit:
        workshop_template = Article.objects.get(
            name="template_for_workshop_page").content

        if not workshop:
            initial_workshop = Workshop()
            initial_workshop.year = year
        else:
            initial_workshop = workshop

        if request.method == 'POST' and 'qualify' not in request.POST:
            if not has_perm_to_edit:
                return HttpResponseForbidden()
            form = WorkshopForm(request.POST, request.FILES, workshop_url=workshop_url,
                                instance=initial_workshop, has_perm_to_edit=has_perm_to_edit,
                                has_perm_to_disable_uploads=request.user.has_perm('wwwapp.edit_all_workshops'),
                                profile_warnings=profile_warnings)
            if form.is_valid():
                new = workshop is None
                workshop = form.save(commit=False)
                if new:
                    assert workshop.year == year
                if workshop.page_content == workshop_template:
                    # If the workshop page was not filled in, do not save the template to db
                    workshop.page_content = ""
                workshop.save()
                form.save_m2m()
                if new:
                    user_profile = UserProfile.objects.get(user=request.user)
                    workshop.lecturer.add(user_profile)
                    workshop.save()
                if new:
                    messages.info(request, format_html(
                        'Twoje zgłoszenie zostało zapisane. Jego status i możliwość dalszej edycji znajdziesz w zakładce "<a href="{}">Status kwalifikacji</a>"',
                        reverse('mydata_status')
                    ))
                else:
                    messages.info(request, 'Zapisano.', extra_tags='auto-dismiss')
                return redirect('workshop_edit', form.instance.year.pk, form.instance.name)
        else:
            if workshop and workshop.is_publicly_visible() and not workshop.page_content:
                workshop.page_content = workshop_template
            form = WorkshopForm(instance=initial_workshop, workshop_url=workshop_url, has_perm_to_edit=has_perm_to_edit,
                                has_perm_to_disable_uploads=request.user.has_perm('wwwapp.edit_all_workshops'),
                                profile_warnings=profile_warnings)
    else:
        form = None

    context = {}
    context['title'] = title
    context['workshop'] = workshop
    context['is_lecturer'] = is_lecturer
    context['has_perm_to_edit'] = has_perm_to_edit
    context['has_perm_to_view_details'] = has_perm_to_edit or has_perm_to_see_all
    context['are_proposals_open'] = year.are_proposals_open()

    context['form'] = form

    return render(request, 'workshopedit.html', context)


def legacy_workshop_redirect_view(request, name):
    # To keep the old links working
    # Workshops from editions <= 2020 should be unique
    workshop = get_object_or_404(Workshop.objects.filter(name=name).order_by('year')[:1])
    return redirect('workshop_page', workshop.year.pk, workshop.name, permanent=True)


def legacy_qualification_problems_redirect_view(request, name):
    # To keep the old links working
    # Workshops from editions <= 2020 should be unique
    workshop = get_object_or_404(Workshop.objects.filter(name=name).order_by('year')[:1])
    return redirect('qualification_problems', workshop.year.pk, workshop.name, permanent=True)


@login_required()
def workshop_participants_view(request, year, name):
    workshop = get_object_or_404(Workshop, year__pk=year, name=name)
    has_perm_to_edit, is_lecturer = can_edit_workshop(workshop, request.user)

    if not workshop.is_publicly_visible():  # Accepted or cancelled
        return HttpResponseForbidden("Warsztaty nie zostały zaakceptowane")

    if not (has_perm_to_edit or request.user.has_perm('wwwapp.see_all_workshops')):
        return HttpResponseForbidden()

    context = {}
    context['title'] = workshop.title
    context['workshop'] = workshop
    context['is_lecturer'] = is_lecturer
    context['has_perm_to_edit'] = has_perm_to_edit
    context['has_perm_to_view_details'] = True

    context['workshop_participants'] = workshop.participants.select_related(
            'workshop', 'workshop__year', 'camp_participation__user_profile', 'camp_participation__user_profile__user', 'solution').order_by('id')

    for participant in context['workshop_participants']:
        participant.form = WorkshopParticipantPointsForm(instance=participant, auto_id='%s_'+str(participant.id))
    
    return render(request, 'workshopparticipants.html', context)


@require_POST
def save_points_view(request):
    if 'id' not in request.POST:
        raise SuspiciousOperation()

    workshop_participant = WorkshopParticipant.objects.get(id=request.POST['id'])

    has_perm_to_edit, _is_lecturer = can_edit_workshop(workshop_participant.workshop, request.user)
    if not has_perm_to_edit:
        return HttpResponseForbidden()

    if not workshop_participant.workshop.is_qualifying:
        return HttpResponseForbidden("Na te warsztaty nie obowiązuje kwalifikacja")

    if workshop_participant.workshop.solution_uploads_enabled and not hasattr(workshop_participant, 'solution'):
        return HttpResponseForbidden("Nie przesłano rozwiązań")

    form = WorkshopParticipantPointsForm(request.POST, instance=workshop_participant)
    if not form.is_valid():
        return JsonResponse({'error': form.errors.as_text()})
    workshop_participant = form.save()
    workshop_participant = WorkshopParticipant.objects.get(pk=workshop_participant.pk)  # refresh the is_qualified field

    return JsonResponse({'qualification_result': workshop_participant.qualification_result,
                         'comment': workshop_participant.comment,
                         'mark': qualified_mark(workshop_participant.is_qualified)})


def _people_datatable(request: HttpRequest, year: Optional[Camp], participants: QuerySet[UserProfile],
                      interested: QuerySet[str], all_forms: QuerySet[Form], context: Dict[str, Any]) -> HttpResponse:
    participants = participants \
        .select_related('user') \
        .prefetch_related(
        'camp_participation',
        'camp_participation__year',
        'lecturer_workshops',
        'lecturer_workshops__year',
    )

    if year is not None:
        participants = participants.prefetch_related(
            Prefetch('camp_participation__workshop_participation',
                     queryset=WorkshopParticipant.objects.filter(camp_participation__year=year)),
            'camp_participation__workshop_participation__solution',
            'camp_participation__workshop_participation__workshop',
            'camp_participation__workshop_participation__workshop__year',
        )

    all_forms = all_forms.prefetch_related('questions')
    all_questions = [question for form in all_forms for question in form.questions.all()]
    all_answers = FormQuestionAnswer.objects.prefetch_related('question', 'user').filter(
        user__user_profile__in=participants, question__in=all_questions).all()

    # Group the answers by users
    user_answers = {}
    for answer in all_answers:
        if answer.user.pk not in user_answers:
            user_answers[answer.user.pk] = []
        user_answers[answer.user.pk].append(answer)

    people = []

    for participant in participants:
        # Arrange the answers array such that the answer at index i matches the question i
        answers = [next(filter(lambda a: a.question.pk == question.pk, user_answers.get(participant.user.pk, [])), None)
                   for question in all_questions]

        birth_field = year.form_question_birth_date if year else None
        birth_answer = next(filter(lambda x: x[0] == birth_field and x[1] and x[1].value, zip(all_questions, answers)),
                            None) if birth_field else None
        if birth_answer and birth_answer[0].data_type == FormQuestion.TYPE_PESEL:
            birth = birth_answer[1].pesel_extract_date()
        elif birth_answer and birth_answer[0].data_type == FormQuestion.TYPE_DATE:
            birth = birth_answer[1].value_date
        else:
            birth = None

        is_adult = None
        if birth is not None:
            if year is not None and year.start_date:
                is_adult = year.start_date >= birth + relativedelta(years=18)
            else:
                is_adult = datetime.date.today() >= birth + relativedelta(years=18)

        camp_participation = None
        if year is not None:
            camp_participation = participant.camp_participation.all()
            camp_participation = list(filter(lambda x: x.year == year, camp_participation))
            camp_participation = camp_participation[0] if camp_participation else None

        participation_data = participant.all_participation_data()
        if not request.user.has_perm('wwwapp.see_all_workshops'):
            # If the current user can't see non-public workshops, remove them from the list
            for participation in participation_data:
                participation['workshops'] = [w for w in participation['workshops'] if w.is_publicly_visible()]

        person = {
            'user': participant.user,
            'email': participant.user.email,
            'workshops': filter(lambda x: year is not None and x.year == year, participant.lecturer_workshops.all()),
            'gender': participant.get_gender_display(),
            'is_adult': is_adult,
            'matura_exam_year': participant.matura_exam_year,
            'workshop_count': camp_participation.workshop_count if camp_participation else 0,
            'solution_count': camp_participation.solution_count if camp_participation else 0,
            'checked_solution_count': camp_participation.checked_solution_count if camp_participation else 0,
            'to_be_checked_solution_count': camp_participation.to_be_checked_solution_count if camp_participation else 0,
            'accepted_workshop_count': camp_participation.accepted_workshop_count if camp_participation else 0,
            'checked_solution_percentage': camp_participation.checked_solution_percentage if camp_participation else -1,
            'has_completed_profile': participant.is_completed,
            'has_cover_letter': len(camp_participation.cover_letter) > 50 if camp_participation else None,
            'status': camp_participation.status if camp_participation else None,
            'status_display': camp_participation.get_status_display if camp_participation else None,
            'participation_data': participation_data,
            'school': participant.school,
            'points': camp_participation.result_in_percent if camp_participation else 0.0,
            'infos': [],
            'how_do_you_know_about': participant.how_do_you_know_about,
            'form_answers': zip(all_questions, answers),
        }

        if year and camp_participation is not None:
            for wp in camp_participation.workshop_participation.all():
                if not wp.workshop.is_qualifying:
                    person['infos'].append((-3, "{title} : Warsztaty bez kwalifikacji".format(
                        title=wp.workshop.title
                    )))
                elif wp.workshop.solution_uploads_enabled and not hasattr(wp, 'solution'):
                    person['infos'].append((-2, "{title} : Nie przesłano rozwiązań".format(
                        title=wp.workshop.title
                    )))
                elif wp.qualification_result is None:
                    person['infos'].append((-1, "{title} : Jeszcze nie sprawdzone".format(
                        title=wp.workshop.title
                    )))
                else:
                    person['infos'].append((wp.result_in_percent, "{title} : {result:.1f}%".format(
                        title=wp.workshop.title,
                        result=wp.result_in_percent
                    )))
            person['infos'] = list(map(lambda x: x[1], sorted(person['infos'], key=lambda x: x[0], reverse=True)))
        people.append(person)

    for email in interested:
        person = {
            'user': None,
            'email': email,
            'workshops': [],
            'gender': None,
            'is_adult': None,
            'matura_exam_year': None,
            'workshop_count': 0,
            'solution_count': 0,
            'checked_solution_count': 0,
            'to_be_checked_solution_count': 0,
            'accepted_workshop_count': 0,
            'checked_solution_percentage': -1,
            'has_completed_profile': False,
            'has_cover_letter': None,
            'status': None,
            'status_display': None,
            'participation_data': [],
            'school': '',
            'points': 0.0,
            'infos': [],
            'how_do_you_know_about': '',
            'form_answers': zip(all_questions, [None for _ in all_questions]),
        }
        people.append(person)

    context = context.copy()
    context['people'] = people
    context['form_questions'] = all_questions
    return render(request, 'listpeople.html', context)

@login_required()
@permission_required('wwwapp.see_all_users', raise_exception=True)
def participants_view(request: HttpRequest, year: Optional[int] = None) -> HttpResponse:
    if year is not None:
        year = get_object_or_404(Camp, pk=year)
        participants = UserProfile.objects.filter(camp_participation__year=year)
        participants = participants.exclude(lecturer_workshops__in=Workshop.objects.filter(year=year, status=Workshop.STATUS_ACCEPTED))
        interested = CampInterestEmail.objects.filter(year=year)
    else:
        participants = UserProfile.objects.all()
        interested = CampInterestEmail.objects.all()
    interested = interested.exclude(email__in=[p.user.email for p in participants])
    interested = interested.values_list('email', flat=True).distinct()

    if year is not None:
        # Participants view only displays forms for the selected year
        all_forms = year.forms.all()
    else:
        # All people view only displays forms not bound to any year
        all_forms = Form.objects.filter(years=None)

    return _people_datatable(request, year, participants, interested, all_forms, {
        'selected_year': year,
        'title': ('Uczestnicy: %s' % year) if year is not None else 'Wszyscy ludzie',
        'is_all_people': year is None,
        'is_lecturers': False
    })


@login_required()
@permission_required('wwwapp.see_all_users', raise_exception=True)
def lecturers_view(request: HttpRequest, year: int) -> HttpResponse:
    year = get_object_or_404(Camp, pk=year)

    lecturers = UserProfile.objects.filter(lecturer_workshops__in=Workshop.objects.filter(year=year, status=Workshop.STATUS_ACCEPTED)).distinct()
    interested = CampInterestEmail.objects.none()

    return _people_datatable(request, year, lecturers, interested, year.forms.all(), {
        'selected_year': year,
        'title': 'Prowadzący: %s' % year,
        'is_all_people': False,
        'is_lecturers': True
    })


@require_POST
def register_to_workshop_view(request, year, name):
    if not request.user.is_authenticated:
        return JsonResponse({'redirect': reverse('login'), 'error': u'Jesteś niezalogowany'})

    workshop = get_object_or_404(Workshop.objects.prefetch_related('lecturer', 'lecturer__user', 'type', 'category'), year__pk=year, name=name)

    if not workshop.is_qualification_editable():
        return JsonResponse({'error': u'Kwalifikacja na te warsztaty została zakończona.'})

    camp_participation, _ = CampParticipant.objects.get_or_create(user_profile=request.user.user_profile, year=workshop.year)
    _, created = camp_participation.workshop_participation.get_or_create(camp_participation=camp_participation, workshop=workshop)

    context = {}
    context['workshop'] = workshop
    context['registered'] = True
    content = render(request, '_programworkshop.html', context).content.decode()
    if created:
        return JsonResponse({'content': content})
    else:
        return JsonResponse({'content': content, 'error': u'Już jesteś zapisany na te warsztaty'})


@require_POST
def unregister_from_workshop_view(request, year, name):
    if not request.user.is_authenticated:
        return JsonResponse({'redirect': reverse('login'), 'error': u'Jesteś niezalogowany'})

    workshop = get_object_or_404(Workshop.objects.prefetch_related('lecturer', 'lecturer__user', 'type', 'category'), year__pk=year, name=name)
    workshop_participant = workshop.participants.filter(camp_participation__user_profile=request.user.user_profile).first()

    if not workshop.is_qualification_editable():
        return JsonResponse({'error': u'Kwalifikacja na te warsztaty została zakończona.'})

    if workshop_participant:
        if workshop_participant.qualification_result is not None or workshop_participant.comment:
            return JsonResponse({'error': u'Masz już wyniki z tej kwalifikacji - nie możesz się wycofać.'})

        if hasattr(workshop_participant, 'solution'):
            return JsonResponse({'error': u'Nie możesz wycofać się z warsztatów, na które przesłałeś już rozwiązania.'})

        workshop_participant.delete()

    context = {}
    context['workshop'] = workshop
    context['registered'] = False
    content = render(request, '_programworkshop.html', context).content.decode()
    if workshop_participant:
        return JsonResponse({'content': content})
    else:
        return JsonResponse({'content': content, 'error': u'Nie jesteś zapisany na te warsztaty'})


@login_required()
def workshop_solution(request, year, name, solution_id=None):
    workshop = get_object_or_404(Workshop, year__pk=year, name=name)
    if not workshop.is_publicly_visible():
        return HttpResponseForbidden("Warsztaty nie zostały zaakceptowane")
    if not workshop.can_access_solution_upload():
        return HttpResponseForbidden('Na te warsztaty nie można obecnie przesyłać rozwiązań')
    has_perm_to_edit, is_lecturer = can_edit_workshop(workshop, request.user)

    if solution_id is None:
        # My solution
        try:
            workshop_participant = workshop.participants \
                .prefetch_related('solution', 'camp_participation__user_profile__user') \
                .get(camp_participation__user_profile__user=request.user)
        except WorkshopParticipant.DoesNotExist:
            return HttpResponseForbidden('Nie jesteś zapisany na te warsztaty')
        solution = workshop_participant.solution if hasattr(workshop_participant, 'solution') else None
        if not solution:
            if workshop.are_solutions_editable():
                solution = Solution(workshop_participant=workshop_participant)
            else:
                return HttpResponseForbidden('Nie przesłałeś rozwiązania na te warsztaty')
    else:
        # Selected solution
        if not has_perm_to_edit and not request.user.has_perm('wwwapp.see_all_workshops'):
            return HttpResponseForbidden()
        solution = get_object_or_404(
            Solution.objects
                .prefetch_related('workshop_participant', 'workshop_participant__camp_participation__user_profile__user')
                .filter(workshop_participant__workshop=workshop),
            pk=solution_id)

    is_solution_editable = solution_id is None and workshop.is_qualification_editable()
    form = SolutionForm(instance=solution, is_editable=is_solution_editable)
    formset = SolutionFileFormSet(instance=solution, is_editable=is_solution_editable)
    grading_form = WorkshopParticipantPointsForm(instance=solution.workshop_participant, participant_view=solution_id is None)

    if request.method == 'POST' and solution_id is None:
        if not is_solution_editable:
            return HttpResponseForbidden()
        form = SolutionForm(request.POST, request.FILES, instance=solution, is_editable=is_solution_editable)
        formset = SolutionFileFormSet(request.POST, request.FILES, instance=solution, is_editable=is_solution_editable)
        if form.is_valid() and formset.is_valid():
            form.save()
            formset.save()
            messages.info(request, 'Zapisano.', extra_tags='auto-dismiss')
            return redirect('workshop_my_solution', year, name)
    if request.method == 'POST' and solution_id is not None:
        if not has_perm_to_edit:
            return HttpResponseForbidden()
        grading_form = WorkshopParticipantPointsForm(request.POST, instance=solution.workshop_participant)
        if grading_form.is_valid():
            grading_form.save()
            messages.info(request, 'Zapisano.')
            return redirect('workshop_solution', year, name, solution.pk)

    context = {}
    context['title'] = workshop.title
    context['workshop'] = workshop
    context['solution'] = solution
    context['form'] = form
    context['form_attachments'] = formset
    context['grading_form'] = grading_form
    context['is_editable'] = is_solution_editable
    context['is_mine'] = solution_id is None
    context['is_lecturer'] = is_lecturer
    context['has_perm_to_edit'] = has_perm_to_edit
    context['has_perm_to_view_details'] = has_perm_to_edit or request.user.has_perm('wwwapp.see_all_workshops')
    return render(request, 'workshopsolution.html', context)


@login_required()
def workshop_solution_file(request, year, name, file_pk, solution_id=None):
    workshop = get_object_or_404(Workshop, year__pk=year, name=name)
    if not workshop.is_publicly_visible():
        return HttpResponseForbidden("Warsztaty nie zostały zaakceptowane")
    if not workshop.can_access_solution_upload():
        return HttpResponseForbidden('Na te warsztaty nie można obecnie przesyłać rozwiązań')

    if not solution_id:
        # My solution
        try:
            workshop_participant = workshop.participants \
                .select_related('solution', 'camp_participation__user_profile__user') \
                .get(camp_participation__user_profile__user=request.user)
        except WorkshopParticipant.DoesNotExist:
            return HttpResponseForbidden('Nie jesteś zapisany na te warsztaty')
        solution = workshop_participant.solution if hasattr(workshop_participant, 'solution') else None
        if not solution:
            return HttpResponseForbidden('Nie przesłałeś rozwiązania na te warsztaty')
    else:
        # Selected solution
        has_perm_to_edit, _is_lecturer = can_edit_workshop(workshop, request.user)
        if not has_perm_to_edit and not request.user.has_perm('wwwapp.see_all_workshops'):
            return HttpResponseForbidden()
        solution = get_object_or_404(
            Solution.objects
                .select_related('workshop_participant', 'workshop_participant__camp_participation__user_profile__user')
                .filter(workshop_participant__workshop=workshop),
            pk=solution_id)

    solution_file = get_object_or_404(solution.files.all(), pk=file_pk)

    # Guess the mimetype based on the extension. This is what sendfile does by default, but we want to override the value in some cases.
    mimetype, encoding = mimetypes.guess_type(solution_file.file.path)
    if not mimetype:
        mimetype = 'application/octet-stream'

    # Only some file extensions are allowed to be viewed inline. Force a file download if it's not one of them.
    attachment = False
    if mimetype not in ['application/pdf', 'image/png', 'image/jpeg']:
        mimetype = 'application/octet-stream'
        attachment = True

    return sendfile(request, solution_file.file.path, mimetype=mimetype, encoding=encoding, attachment=attachment)


@permission_required('wwwapp.export_workshop_registration')
def data_for_plan_view(request, year: int) -> HttpResponse:
    year = get_object_or_404(Camp, pk=year)

    data = {}

    participant_profiles_raw = UserProfile.objects.filter(camp_participation__year=year, camp_participation__status='Z')

    lecturer_profiles_raw = set()
    workshop_ids = set()
    workshops = []
    for workshop in Workshop.objects.filter(status='Z', year=year):
        workshop_data = {'wid': workshop.id,
                         'name': workshop.title,
                         'lecturers': [lect.id for lect in
                                       workshop.lecturer.all()]}
        for lecturer in workshop.lecturer.all():
            if lecturer not in participant_profiles_raw:
                lecturer_profiles_raw.add(lecturer)
        workshop_ids.add(workshop.id)
        workshops.append(workshop_data)
    data['workshops'] = workshops

    users = []
    user_ids = set()

    def clean_date(date: datetime.date or None, min: datetime.date, max: datetime.date, default: datetime.date) -> datetime.date:
        if date is None or (min is not None and date < min) or (max is not None and date > max):
            return default
        return date

    current_year = Camp.current()
    for user_type, profiles in [('Lecturer', lecturer_profiles_raw),
                                ('Participant', participant_profiles_raw)]:
        for up in profiles:
            user = {
                'uid': up.id,
                'name': up.user.get_full_name(),
                'type': user_type,
            }
            users.append(user)
            user_ids.add(up.id)

    if year.form_question_arrival_date and year.form_question_departure_date:
        start_dates = {answer.user.user_profile.id: answer.value_date for answer in year.form_question_arrival_date.answers.prefetch_related('user', 'user__user_profile').filter(question__form__is_visible=True, user__user_profile__in=user_ids, value_date__isnull=False)}
        end_dates = {answer.user.user_profile.id: answer.value_date for answer in year.form_question_departure_date.answers.prefetch_related('user', 'user__user_profile').filter(question__form__is_visible=True, user__user_profile__in=user_ids, value_date__isnull=False)}

        for user in users:
            start_date = start_dates[user['uid']] if user['uid'] in start_dates else None
            end_date = end_dates[user['uid']] if user['uid'] in end_dates else None

            user.update({
                'start': clean_date(start_date, year.start_date, year.end_date, year.start_date),
                'end': clean_date(end_date, year.start_date, year.end_date, year.end_date)
            })

    data['users'] = users

    participation = []
    for wp in WorkshopParticipant.objects.filter(workshop__id__in=workshop_ids, camp_participation__user_profile__id__in=user_ids):
        participation.append({
            'wid': wp.workshop.id,
            'uid': wp.camp_participation.user_profile.id,
        })
    data['participation'] = participation

    return JsonResponse(data, json_dumps_params={'indent': 4})


def qualification_problems_view(request, year, name):
    workshop = get_object_or_404(Workshop, year__pk=year, name=name)

    if not workshop.is_publicly_visible():  # Accepted or cancelled
        return HttpResponseForbidden("Warsztaty nie zostały zaakceptowane")
    if not workshop.is_qualifying:
        return HttpResponseNotFound("Na te warsztaty nie ma kwalifikacji")
    if not workshop.qualification_problems:
        return HttpResponseNotFound("Nie ma jeszcze zadań kwalifikacyjnych")

    mimetype, encoding = mimetypes.guess_type(workshop.qualification_problems.path)
    if mimetype != 'application/pdf':
        raise SuspiciousOperation('Zadania kwalifikacyjne nie są PDFem')

    return sendfile(request, workshop.qualification_problems.path, mimetype='application/pdf')


def article_view(request, name):
    context = {}

    art = get_object_or_404(Article, name=name)
    title = art.title
    can_edit_article = request.user.has_perm('wwwapp.change_article')

    bleach_args = get_bleach_default_options().copy()
    if art.name == 'index':
        bleach_args['tags'] += ['iframe']  # Allow iframe on main page for Facebook embed
    article_content_clean = mark_safe(bleach.clean(art.content, **bleach_args))

    context['title'] = title
    context['article'] = art
    context['article_content_clean'] = article_content_clean
    context['can_edit'] = can_edit_article

    return render(request, 'article.html', context)


@login_required()
def article_edit_view(request, name=None):
    context = {}
    new = (name is None)
    if new:
        art = None
        title = 'Nowy artykuł'
        has_perm = request.user.has_perm('wwwapp.add_article')
    else:
        art = get_object_or_404(Article, name=name)
        title = art.title
        has_perm = request.user.has_perm('wwwapp.change_article')

    if not has_perm:
        return HttpResponseForbidden()

    article_url = request.build_absolute_uri(
        reverse('article', kwargs={'name': 'SOMENAME'}))
    article_url = article_url.split('SOMENAME')
    if art and art.name in ArticleForm.SPECIAL_ARTICLES.keys():
        title = ArticleForm.SPECIAL_ARTICLES[art.name]

    if request.method == 'POST':
        form = ArticleForm(request.user, article_url, request.POST, instance=art)
        if form.is_valid():
            article = form.save(commit=False)
            article.modified_by = request.user
            article.save()
            form.save_m2m()
            messages.info(request, 'Zapisano.', extra_tags='auto-dismiss')
            return redirect('article', form.instance.name)
    else:
        form = ArticleForm(request.user, article_url, instance=art)

    context['title'] = title
    context['article'] = art
    context['form'] = form

    return render(request, 'articleedit.html', context)


def article_name_list_view(request):
    articles = Article.objects.all()
    article_list = [{'title': 'Artykuł: ' + (article.title or article.name), 'value': reverse('article', kwargs={'name': article.name})} for article in articles]

    workshops = Workshop.objects.filter(Q(status='Z') | Q(status='X')).order_by('-year')
    workshop_list = [{'title': 'Warsztaty (' + str(workshop.year) + '): ' + workshop.title, 'value': reverse('workshop_page', kwargs={'year': workshop.year.pk, 'name': workshop.name})} for workshop in workshops]

    return JsonResponse(article_list + workshop_list, safe=False)


@login_required()
@permission_required('wwwapp.see_all_workshops', raise_exception=True)
def workshops_view(request, year):
    year = get_object_or_404(Camp, pk=year)

    context = {}
    context['workshops'] = year.workshops.with_counts().prefetch_related(
        'year',
        'lecturer',
        'lecturer__user',
        'type',
        'type__year',
        'category',
        'category__year',
    ).all()
    context['title'] = 'Warsztaty: %s' % year

    context['selected_year'] = year
    return render(request, 'listworkshop.html', context)


def as_article(name):
    # We want to make sure that article with this name exists.
    # try-except is needed because of some migration/initialization problems.
    try:
        Article.objects.get_or_create(name=name)
    except OperationalError:
        print("WARNING: Couldn't create article named", name,
              "; This should happen only during migration.", file=sys.stderr)
    except ProgrammingError:
        print("WARNING: Couldn't create article named", name,
              "; This should happen only during migration.", file=sys.stderr)

    def page(request):
        return article_view(request, name)
    return page


index_view = as_article("index")
template_for_workshop_page_view = as_article("template_for_workshop_page")


def resource_auth_view(request):
    """
    View checking permission for resource (header X-Original-URI). Returns 200
    when currently logged in user should be granted access to resource, 403
    when access should be denied and 401 if the user is not logged in, with a
    Location header if applicable.

    Using a Location header with 403 is kinda weird, but this is a requirement
    of auth_request in NGINX. The NGINX config rewrites it to a proper redirect
    later.

    See https://docs.nginx.com/nginx/admin-guide/security-controls/configuring-subrequest-authentication/
    for intended usage.
    """

    uri = request.META.get('HTTP_X_ORIGINAL_URI', '')

    if not request.user.is_authenticated:
        # Response to auth_request in NGINX has to be 200, 401 or 403
        # We rewrite this to a redirect again in nginx config
        r = redirect_to_login(uri)
        r.status_code = 401
        return r

    if request.user.has_perm('wwwapp.access_all_resources'):
        return HttpResponse("Glory to WWW and the ELITARNY MIMUW!!!")

    user_profile = UserProfile.objects.get(user=request.user)

    for resource in ResourceYearPermission.resources_for_uri(uri):
        if user_profile.is_participating_in(resource.year):
            return HttpResponse("Welcome!")
    return HttpResponseForbidden("What about NO!")


def _upload_file(request, target_dir):
    """
    Handle a file upload from TinyMCE
    """

    form = TinyMCEUpload(request.POST, request.FILES)
    if not form.is_valid():
        data = {'errors': [v for k, v in form.errors.items()]}
        return HttpResponseBadRequest(json.dumps(data))

    os.makedirs(os.path.join(settings.MEDIA_ROOT, target_dir), exist_ok=True)

    f = request.FILES['file']

    h = hashlib.sha256()
    for chunk in f.chunks():
        h.update(chunk)
    h = h.hexdigest()

    name = h + os.path.splitext(f.name)[1]

    with open(os.path.join(settings.MEDIA_ROOT, target_dir, name), 'wb+') as destination:
        for chunk in f.chunks():
            destination.write(chunk)

    return JsonResponse({'location': urljoin(urljoin(settings.MEDIA_URL, target_dir), name)})


@login_required()
@require_POST
@csrf_exempt
def article_edit_upload_file(request, name):
    article = get_object_or_404(Article, name=name)
    target_dir = "images/articles/{}/".format(article.name)
    if not request.user.has_perm('wwwapp.change_article'):
        return HttpResponseForbidden()

    return _upload_file(request, target_dir)


@login_required()
@require_POST
@csrf_exempt
def workshop_edit_upload_file(request, year, name):
    workshop = get_object_or_404(Workshop, year__pk=year, name=name)
    has_perm_to_edit, _is_lecturer = can_edit_workshop(workshop, request.user)
    if not has_perm_to_edit or not workshop.is_publicly_visible() or not workshop.is_workshop_editable():
        return HttpResponseForbidden()
    target_dir = "images/workshops/{}/{}/".format(workshop.year.pk, workshop.name)

    return _upload_file(request, target_dir)
