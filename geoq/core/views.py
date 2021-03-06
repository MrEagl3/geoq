# -*- coding: utf-8 -*-
# This technical data was produced for the U. S. Government under Contract No. W15P7T-13-C-F600, and
# is subject to the Rights in Technical Data-Noncommercial Items clause at DFARS 252.227-7013 (FEB 2012)

import json
import requests

from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User, Group
from django.contrib.gis.geos import GEOSGeometry
from django.core.urlresolvers import reverse, reverse_lazy
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django.forms.util import ValidationError
from django.http import Http404, HttpResponse, HttpResponseRedirect, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import DetailView, ListView, TemplateView, View, DeleteView, CreateView, UpdateView
from datetime import datetime

from models import Project, Job, AOI, Comment, AssigneeType, Organization
from geoq.maps.models import *
from utils import send_assignment_email, increment_metric
from geoq.training.models import Training

from geoq.mgrs.utils import Grid, GridException, GeoConvertException
from geoq.core.utils import send_aoi_create_event
from geoq.core.middleware import Http403
from geoq.mgrs.exceptions import ProgramException
from guardian.decorators import permission_required
from kml_view import *
from shape_view import *


class Dashboard(TemplateView):

    template_name = 'core/dashboard.html'

    def get_context_data(self, **kwargs):
        cv = super(Dashboard, self).get_context_data(**kwargs)

        all_projects = Project.objects.all()
        cv['projects'] = []
        cv['projects_archived'] = []
        cv['projects_exercise'] = []
        cv['projects_private'] = []

        cv['count_users'] = User.objects.count()
        cv['count_jobs'] = Job.objects.count()
        cv['count_workcells_total'] = AOI.objects.count()
        cv['count_training'] = Training.objects.count()

        for project in all_projects:
            if project.private:
                if (self.request.user in project.project_admins.all()) or (self.request.user in project.contributors.all()):
                    cv['projects_private'].append(project)

            elif not project.active:
                cv['projects_archived'].append(project)
            elif project.project_type == 'Exercise':
                cv['projects_exercise'].append(project)
            else:
                cv['projects'].append(project)

        cv['count_projects_active'] = len(cv['projects'])
        cv['count_projects_archived'] = len(cv['projects_archived'])
        cv['count_projects_exercise'] = len(cv['projects_exercise'])

        cv['orgs'] = Organization.objects.filter(show_on_front=True)

        return cv


class BatchCreateAOIS(TemplateView):
    """
    Reads GeoJSON from post request and creates AOIS for each features.
    """
    template_name = 'core/job_batch_create_aois.html'

    def get_context_data(self, **kwargs):
        cv = super(BatchCreateAOIS, self).get_context_data(**kwargs)
        cv['object'] = get_object_or_404(Job, pk=self.kwargs.get('job_pk'))
        return cv

    def post(self, request, *args, **kwargs):
        aois = request.POST.get('aois')
        job = Job.objects.get(id=self.kwargs.get('job_pk'))

        try:
            aois = json.loads(aois)
        except ValueError:
            raise ValidationError(_("Enter valid JSON"))

        response = AOI.objects.bulk_create([AOI(name=job.name,
                                            job=job,
                                            description=job.description,
                                            properties=aoi.get('properties'),
                                            polygon=GEOSGeometry(json.dumps(aoi.get('geometry')))) for aoi in aois])

        return HttpResponse()


#TODO: Abstract this
class DetailedListView(ListView):
    """
    A mixture between a list view and detailed view.
    """

    paginate_by = 15
    model = Project

    def get_queryset(self):
        return Job.objects.filter(project=self.kwargs.get('pk'))

    def get_context_data(self, **kwargs):
        cv = super(DetailedListView, self).get_context_data(**kwargs)
        cv['object'] = get_object_or_404(self.model, pk=self.kwargs.get('pk'))
        return cv


class UserAllowedMixin(object):

    def check_user(self, user, pk):
        return True

    def user_check_failed(self, request, *args, **kwargs):
        message = kwargs['error'] if 'error' in kwargs else "We're sorry, but you are not authorized to access that particular workcell"
        raise Http403(message)

    def dispatch(self, request, *args, **kwargs):
        if not self.check_user(request.user, kwargs):
            return self.user_check_failed(request, *args, **kwargs)
        return super(UserAllowedMixin, self).dispatch(request, *args, **kwargs)


class CreateFeaturesView(UserAllowedMixin, DetailView):
    template_name = 'core/edit.html'
    queryset = AOI.objects.all()
    user_check_failure_path = ''

    def check_user(self, user, kwargs):
        try:
            aoi = AOI.objects.get(id=kwargs.get('pk'))
        except ObjectDoesNotExist:
            return False

        is_admin = False
        if self.request.user.is_superuser or self.request.user.groups.filter(name='admin_group').count() > 0:
            is_admin = True

        if not is_admin:
            courses = aoi.job.required_courses.all()
            if courses:
                classes_passed = True
                failed_names = []
                for course in courses:
                    if user not in course.users_completed.all():
                        classes_passed = False
                        url_name = "<a href='"+reverse_lazy('course_view_information', pk=course.id)+"' target='_blank'>"+course.name+"</a>"
                        failed_names.append(url_name)
                if not classes_passed:
                    courses = ', '.join(failed_names)
                    kwargs['error'] = "This Job has required training courses that you have not passed. Please take these quizes before editing this workcell: "+courses
                    return False

        # logic for what we'll allow
        if aoi.status == 'Unassigned':
            aoi.analyst = self.request.user
            aoi.status = 'In work'
            aoi.save()
            return True
        elif aoi.status == 'In work':
            if is_admin:
                return True
            elif aoi.analyst != self.request.user:
                kwargs['error'] = "Another analyst is already working on this workcell. Please select another workcell"
                return False
            else:
                return True
        elif aoi.status == 'Awaiting review':
            if self.request.user in aoi.job.reviewers.all() or is_admin:
                aoi.status = 'In review'
                aoi.reviewers.add(self.request.user)
                aoi.save()
                increment_metric('workcell_analyzed')
                return True
            else:
                kwargs['error'] = "Sorry, you have not been assigned as a reviewer for this workcell and are not allowed to edit it."
                return False
        elif aoi.status == 'In review':
            # if this user previously reviewed this workcell, allow them in
            if self.request.user in aoi.reviewers.all() or is_admin:
                return True
            else:
                kwargs['error'] = "Sorry, only reviewers who previously reviewed this workcell may have access. You are not allowed to edit it while it is in review."
                return False
        elif is_admin:
            return True
        else:
            # Can't open a completed workcell
            kwargs['error'] = "Sorry, this workcell has been completed and can no longer be edited."
            return False

    def get_context_data(self, **kwargs):
        cv = super(CreateFeaturesView, self).get_context_data(**kwargs)
        cv['reviewers'] = kwargs['object'].job.reviewers.all()
        cv['admin'] = self.request.user.is_superuser or self.request.user.groups.filter(name='admin_group').count() > 0

        if self.object.job.map:
            cv['map'] = self.object.job.map
        else:
            maps = Map.objects.all()
            if maps and len(maps):
                cv['map'] = maps[0]
            else:
                new_default_map = Map(name="Default Map", description="Default Map that should have the layers you want on most maps")
                new_default_map.save()
                cv['map'] = new_default_map

        cv['feature_types'] = self.object.job.feature_types.all() #.order_by('name').order_by('order').order_by('-category')
        cv['feature_types_all'] = FeatureType.objects.all()
        layers = cv['map'].to_object()

        for job in self.object.job.project.jobs:
            if not job.id == self.object.job.id:
                description = "Job #" + str(job.id) + " - " + str(job.name) + ". From Project #" + str(self.object.job.project.id)

                url = self.request.build_absolute_uri(reverse('json-job-grid', args=[job.id]))
                layer_name = "Workcells: " + job.name
                layer = dict(attribution="GeoQ Job Workcells", description=description, id=int(10000+job.id),
                             layer=layer_name, name=layer_name, opacity=0.3, refreshrate=300, shown=False,
                             spatialReference="EPSG:4326", transparent=True, type="GeoJSON", url=url, job=job.id)
                layers['layers'].append(layer)

                url = self.request.build_absolute_uri(reverse('geojson-job-features', args=[job.id]))
                layer_name = "Features: " + job.name
                layer = dict(attribution="GeoQ Job Features", description=description, id=int(20000+job.id),
                             layer=layer_name, name=layer_name, opacity=0.8, refreshrate=300, shown=False,
                             spatialReference="EPSG:4326", transparent=True, type="GeoJSON", url=url, job=job.id)
                layers['layers'].append(layer)

#TODO: Add Job specific layers, and add these here

        cv['layers_on_map'] = json.dumps(layers)

        Comment(user=cv['aoi'].analyst, aoi=cv['aoi'], text="Workcell opened").save()
        return cv


def redirect_to_unassigned_aoi(request, pk):
    """
    Given a job, redirects the view to an unassigned AOI.  If there are no unassigned AOIs, the user will be redirected
     to the job's absolute url.
    """
    job = get_object_or_404(Job, id=pk)

    try:
        return HttpResponseRedirect(job.aois.filter(status='Unassigned').order_by('priority')[0].get_absolute_url())
    except IndexError:
        return HttpResponseRedirect(job.get_absolute_url())


class JobDetailedListView(ListView):
    """
    A mixture between a list view and detailed view.
    """

    paginate_by = 15
    model = Job
    default_status = 'in work'
    request = None
    metrics = False

    def get_queryset(self):
        status = getattr(self, 'status', None)
        q_set = AOI.objects.filter(job=self.kwargs.get('pk'))

        # # If there is a user logged in, we want to show their stuff
        # # at the top of the list
        if self.request.user.id is not None and status == 'in work':
            user = self.request.user
            clauses = 'WHEN analyst_id=%s THEN %s ELSE 1' % (user.id, 0)
            ordering = 'CASE %s END' % clauses
            self.queryset = q_set.extra(
               select={'ordering': ordering}, order_by=('ordering',))
        else:
            self.queryset = q_set

        if status and (status in [value.lower() for value in AOI.STATUS_VALUES]):
            return self.queryset.filter(status__iexact=status)
        else:
            return self.queryset

    def get(self, request, *args, **kwargs):
        self.status = self.kwargs.get('status')

        if self.status and hasattr(self.status, "lower"):
            self.status = self.status.lower()
        else:
            self.status = self.default_status.lower()

        self.request = request

        return super(JobDetailedListView, self).get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        cv = super(JobDetailedListView, self).get_context_data(**kwargs)
        job_id = self.kwargs.get('pk')
        cv['object'] = get_object_or_404(self.model, pk=job_id)
        cv['statuses'] = AOI.STATUS_VALUES
        cv['active_status'] = self.status
        cv['workcell_count'] = cv['object'].aoi_count()
        cv['metrics'] = self.metrics
        cv['metrics_url'] = reverse('job-metrics', args=[job_id])
        cv['features_url'] = reverse('json-job-features', args=[job_id])
        #TODO: Add feature_count

        if cv['object'].aoi_count() > 0:
            cv['completed'] = (cv['object'].complete().count() * 100) / cv['workcell_count']
        else:
            cv['completed'] = 0
        return cv


class JobDelete(DeleteView):
    model = Job
    template_name = "core/generic_confirm_delete.html"

    def get_success_url(self):
        return reverse('project-detail', args=[self.object.project.pk])


class AOIDelete(DeleteView):
    model = AOI
    template_name = "core/generic_confirm_delete.html"

    def get_success_url(self):
        return reverse('job-detail', args=[self.object.job.pk])


class AOIDetailedListView(ListView):
    """
    A mixture between a list view and detailed view.
    """

    paginate_by = 25
    model = AOI
    default_status = 'unassigned'

    def get_queryset(self):
        status = getattr(self, 'status', None)
        self.queryset = AOI.objects.all()
        if status and (status in [value.lower() for value in AOI.STATUS_VALUES]):
            return self.queryset.filter(status__iexact=status)
        else:
            return self.queryset

    def get(self, request, *args, **kwargs):
        self.status = self.kwargs.get('status')

        if self.status and hasattr(self.status, "lower"):
            self.status = self.status.lower()
        else:
            self.status = self.default_status.lower()

        return super(AOIDetailedListView, self).get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        cv = super(AOIDetailedListView, self).get_context_data(**kwargs)
        cv['statuses'] = AOI.STATUS_VALUES
        cv['active_status'] = self.status
        return cv


class CreateProjectView(CreateView):
    """
    Create view that adds the user that created the job as a reviewer.
    """

    def form_valid(self, form):
        """
        If the form is valid, save the associated model and add the current user as a reviewer.
        """
        self.object = form.save()
        self.object.project_admins.add(self.request.user)
        self.object.save()
        return HttpResponseRedirect(self.get_success_url())


class CreateJobView(CreateView):
    """
    Create view that adds the user that created the job as a reviewer.
    """

    def get_form_kwargs(self):
        kwargs = super(CreateJobView, self).get_form_kwargs()
        kwargs['project'] = self.request.GET['project'] if 'project' in self.request.GET else 0
        return kwargs

    def form_valid(self, form):
        """
        If the form is valid, save the associated model and add the current user as a reviewer.
        """
        self.object = form.save()
        self.object.reviewers.add(self.request.user)
        self.object.save()
        return HttpResponseRedirect(self.get_success_url())


class UpdateJobView(UpdateView):
    """
    Update Job
    """

    def get_form_kwargs(self):
        kwargs = super(UpdateJobView, self).get_form_kwargs()
        kwargs['project'] = kwargs['instance'].project_id if hasattr(kwargs['instance'],'project_id') else 0
        return kwargs


class ChangeAOIStatus(View):
    http_method_names = ['post','get']

    def _get_aoi_and_update(self, pk):
        aoi = get_object_or_404(AOI, pk=pk)
        status = self.kwargs.get('status')
        return status, aoi

    def _update_aoi(self, request, aoi, status):
        aoi.analyst = request.user
        aoi.status = status
        aoi.save()
        return aoi

    def get(self, request, **kwargs):
        # Used to unassign tasks on the job detail, 'in work' tab

        status, aoi = self._get_aoi_and_update(self.kwargs.get('pk'))

        if aoi.user_can_complete(request.user):
            aoi = self._update_aoi(request, aoi, status)
            Comment(aoi=aoi,user=request.user,text='changed status to %s' % status).save()

        return HttpResponseRedirect('/geoq/jobs/%s/' % aoi.job.id)

    def post(self, request, **kwargs):

        status, aoi = self._get_aoi_and_update(self.kwargs.get('pk'))

        if aoi.user_can_complete(request.user):
            aoi = self._update_aoi(request, aoi, status)
            Comment(aoi=aoi,user=request.user,text='changed status to %s' % status).save()

            features_updated = 0
            if 'feature_ids' in request.POST:
                features = request.POST['feature_ids']
                features = str(features)
                if features and len(features):
                    try:
                        feature_ids = tuple([int(x) for x in features.split(',')])
                        feature_list = Feature.objects.filter(id__in=feature_ids)
                        features_updated = feature_list.update(status=status)
                    except ValueError:
                        features_updated = 0

            # send aoi completion event for badging
            send_aoi_create_event(request.user, aoi.id, aoi.features.all().count())
            return HttpResponse(json.dumps({aoi.id: aoi.status, 'features_updated': features_updated}), mimetype="application/json")
        else:
            error = dict(error=403,
                         details="User not allowed to modify the status of this AOI.",)
            return HttpResponse(json.dumps(error), status=error.get('error'))


class PrioritizeWorkcells(TemplateView):
    http_method_names = ['post', 'get']
    template_name = 'core/prioritize_workcells.html'

    def get_context_data(self, **kwargs):
        cv = super(PrioritizeWorkcells, self).get_context_data(**kwargs)
        cv['object'] = get_object_or_404(Job, pk=self.kwargs.get('job_pk'))
        cv['workcells'] = AOI.objects.filter(job_id=self.kwargs.get('job_pk')).order_by('id')
        return cv


    def post(self, request, **kwargs):
        job = get_object_or_404(Job, id=self.kwargs.get('job_pk'))
        idvals = iter(request.POST.getlist('id'))
        prvals = iter(request.POST.getlist('priority'))
        workcells = AOI.objects.filter(job=job)

        for idval in idvals:
            id = int(idval)
            priority = int(next(prvals))
            cell = workcells.get(id=id)
            cell.priority = priority
            cell.save()


        return HttpResponseRedirect(job.get_absolute_url())


class AssignWorkcellsView(TemplateView):
    http_method_names = ['post', 'get']
    template_name = 'core/assign_workcells.html'
    model = Job

    def get_queryset(self):
        status = getattr(self, 'status', None)
        q_set = AOI.objects.filter(job=self.kwargs.get('job_pk'))

    def get_context_data(self, **kwargs):
        cv = super(AssignWorkcellsView, self).get_context_data(**kwargs)
        job_id = self.kwargs.get('job_pk')
        cv['object'] = get_object_or_404(self.model, pk=job_id)
        cv['workcell_count'] = cv['object'].aoi_count()
        return cv

    def post(self, request, **kwargs):
        job_id = self.kwargs.get('job_pk')
        job = get_object_or_404(self.model, pk=job_id)
        workcells = request.POST.getlist('workcells[]')
        utype = request.POST['user_type']
        id = request.POST['user_data']
        send_email = request.POST['email']

        if utype and id and workcells:
            Type = User if utype == 'user' else Group
            keyfield = 'username' if utype == 'user' else 'name'
            q = Q(**{"%s__contains" % keyfield: id})
            user_or_group = Type.objects.filter(q)
            if user_or_group.count() > 0:
                aois = AOI.objects.filter(id__in=workcells)
                for aoi in aois:
                    aoi.assignee_type_id = AssigneeType.USER if utype == 'user' else AssigneeType.GROUP
                    aoi.assignee_id = user_or_group.get().id
                    aoi.status = 'Assigned'
                    aoi.save()

                if send_email:
                    send_assignment_email(user_or_group.get(), job, request)


            return HttpResponse('{"status":"ok"}', status=200)
        else:
            return HttpResponse('{"status":"required field missing"}', status=500)


def usng(request):
    """
    Proxy to USNG service.
    """

    base_url = "http://app01.ozone.nga.mil/geoserver/wfs" #TODO: Move this to settings

    bbox = request.GET.get('bbox')

    if not bbox:
        return HttpResponse()

    params = dict()
    params['service'] = 'wfs'
    params['version'] = '1.0.0'
    params['request'] = 'GetFeature'
    params['typeName'] = 'usng'
    params['bbox'] = bbox
    params['outputFormat'] = 'json'
    params['srsName'] = 'EPSG:4326'
    resp = requests.get(base_url, params=params)
    return HttpResponse(resp, mimetype="application/json")


def mgrs(request):
    """
    Create mgrs grid in manner similar to usng above
    """

    bbox = request.GET.get('bbox')

    if not bbox:
        return HttpResponse()

    bb = bbox.split(',')
    output = ""

    if not len(bb) == 4:
        output = json.dumps(dict(error=500, message='Need 4 corners of a bounding box passed in using EPSG 4386 lat/long format', grid=str(bb)))
    else:
        try:
            grid = Grid(bb[1], bb[0], bb[3], bb[2])
            fc = grid.build_grid_fc()
            output = json.dumps(fc)
        except GridException:
            error = dict(error=500, details="Can't create grids across longitudinal boundaries. Try creating a smaller bounding box",)
            return HttpResponse(json.dumps(error), status=error.get('error'), mimetype="application/json")
        except GeoConvertException, e:
            error = dict(error=500, details="GeoConvert doesn't recognize those cooridnates", exception=str(e))
            return HttpResponse(json.dumps(error), status=error.get('error'), mimetype="application/json")
        except ProgramException, e:
            error = dict(error=500, details="Error executing external GeoConvert application. Make sure it is installed on the server", exception=str(e))
            return HttpResponse(json.dumps(error), status=error.get('error'), mimetype="application/json")
        except Exception, e:
            import traceback
            output = json.dumps(dict(error=500, message='Generic Exception', details=traceback.format_exc(), exception=str(e), grid=str(bb)))

    return HttpResponse(output, mimetype="application/json")


def geocode(request):
    """
    Proxy to geocode service
    """

    base_url = "http://geoservices.tamu.edu/Services/Geocode/WebService/GeocoderWebServiceHttpNonParsed_V04_01.aspx"
    params['apiKey'] = '57956afd728b4204bee23dbb17f00573'
    params['version'] = '4.01'

def aoi_delete(request, pk):
    try:
        aoi = AOI.objects.get(pk=pk)
        aoi.delete()
    except ObjectDoesNotExist:
        raise Http404

    return HttpResponse(status=200)


def display_help(request):
    return render(request, 'core/geoq_help.html')

@permission_required('core.assign_workcells', return_403=True)
def list_users(request, job_pk):
    job = get_object_or_404(Job, pk=job_pk)
    usernames = job.analysts.all().values('username').order_by('username')
    users = []
    for u in usernames:
        users.append(u['username'])

    return HttpResponse(json.dumps(users), mimetype="application/json")

@permission_required('core.assign_workcells', return_403=True)
def list_groups(request, job_pk):
    job = get_object_or_404(Job, pk=job_pk)
    groupnames = job.teams.all().values('name').order_by('name')
    groups = []
    for g in groupnames:
        groups.append(g['name'])

    return HttpResponse(json.dumps(groups), mimetype="application/json")


@login_required
def update_job_data(request, *args, **kwargs):
    aoi_pk = kwargs.get('pk')
    attribute = request.POST.get('id')
    value = request.POST.get('value')
    if attribute and value:
        aoi = get_object_or_404(AOI, pk=aoi_pk)

        if attribute == 'status':
            aoi.status = value
        elif attribute == 'priority':
            aoi.priority = int(value)
        else:
            properties_main = aoi.properties or {}
            properties_main[attribute] = value
            aoi.properties = properties_main

        aoi.save()
        return HttpResponse(value, mimetype="application/json", status=200)
    else:
        return HttpResponse('{"status":"attribute and value not passed in"}', mimetype="application/json", status=400)


@login_required
def update_feature_data(request, *args, **kwargs):
    feature_pk = kwargs.get('pk')
    attribute = request.POST.get('id')
    value = request.POST.get('value')
    if attribute and value:
        feature = get_object_or_404(Feature, pk=feature_pk)

        properties_main = feature.properties or {}

        if attribute == 'add_link':
            if properties_main.has_key('linked_items'):
                properties_main_links = properties_main['linked_items']
            else:
                properties_main_links = []
            link_info = {}
            link_info['properties'] = json.loads(value)
            link_info['created_at'] = str(datetime.now())
            link_info['user'] = str(request.user)
            properties_main_links.append(link_info)
            properties_main['linked_items'] = properties_main_links
            value = json.dumps(link_info)
        elif attribute == 'priority':
            feature.priority = int(value)
            properties_main[attribute] = value
        else:
            properties_main[attribute] = value

        feature.properties = properties_main

        feature.save()
        return HttpResponse(value, mimetype="application/json", status=200)
    else:
        return HttpResponse('{"status":"attribute and value not passed in"}', mimetype="application/json", status=400)


@login_required
def prioritize_cells(request, method, **kwargs):
    aois_data = request.POST.get('aois')
    method = method or "daytime"

    try:
        from random import randrange
        aois = json.loads(aois_data)

        if method == "random":
            for aoi in aois:
                if not 'properties' in aoi:
                    aoi['properties'] = dict()

                aoi['properties']['priority'] = randrange(1, 5)

        output = aois
    except Exception, ex:
        import traceback
        errorCode = 'Program Error: ' + traceback.format_exc()

        log = dict(error='Could not prioritize Work Cells', message=str(ex), details=errorCode, method=method)
        return HttpResponse(json.dumps(log), mimetype="application/json", status=500)

    return HttpResponse(json.dumps(output), mimetype="application/json", status=200)


@login_required
def batch_create_aois(request, *args, **kwargs):
    aois = request.POST.get('aois')
    job = Job.objects.get(id=kwargs.get('job_pk'))

    try:
        aois = json.loads(aois)
    except ValueError:
        raise ValidationError(_("Enter valid JSON"))

    response = AOI.objects.bulk_create([AOI(name=(aoi.get('name')),
                                        job=job,
                                        description=job.description,
                                        properties=aoi.get('properties'),
                                        polygon=GEOSGeometry(json.dumps(aoi.get('geometry')))) for aoi in aois])

    return HttpResponse()

@login_required
def add_workcell_comment(request, *args, **kwargs):
    aoi = get_object_or_404(AOI, id=kwargs.get('pk'))
    user = request.user
    comment_text = request.POST['comment']

    if comment_text:
        comment = Comment(user=user, aoi=aoi, text=comment_text)
        comment.save()

    return HttpResponse()


class LogJSON(ListView):
    model = AOI

    def get(self,request,*args,**kwargs):
        aoi = get_object_or_404(AOI, id=kwargs.get('pk'))
        log = aoi.logJSON()

        return HttpResponse(json.dumps(log), mimetype="application/json", status=200)


class LayersJSON(ListView):
    model = Layer

    def get(self, request, *args, **kwargs):
        Layers = Layer.objects.all()

        objects = []
        for layer in Layers:
            layer_json = dict()
            for field in layer._meta.get_all_field_names():
                if not field in ['created_at', 'updated_at', 'extent', 'objects', 'map_layer_set', 'layer_params']:
                    val = layer.__getattribute__(field)

                    try:
                        flat_val = str(val)
                    except UnicodeEncodeError:
                        flat_val = unicode(val).encode('unicode_escape')

                    layer_json[field] = str(flat_val)

                elif field == 'layer_params':
                    layer_json[field] = layer.layer_params

            objects.append(layer_json)

        out_json = dict(objects=objects)

        return HttpResponse(json.dumps(out_json), mimetype="application/json", status=200)


class CellJSON(ListView):
    model = AOI

    def get(self, request, *args, **kwargs):
        aoi = get_object_or_404(AOI, id=kwargs.get('pk'))
        cell = aoi.grid_geoJSON()

        return HttpResponse(cell, mimetype="application/json", status=200)


class JobGeoJSON(ListView):
    model = Job

    def get(self, request, *args, **kwargs):
        job = get_object_or_404(Job, pk=self.kwargs.get('pk'))
        geojson = job.features_geoJSON()

        return HttpResponse(geojson, mimetype="application/json", status=200)

class JobStyledGeoJSON(ListView):
    model = Job

    def get(self, request, *args, **kwargs):
        job = get_object_or_404(Job, pk=self.kwargs.get('pk'))
        geojson = job.features_geoJSON(using_style_template=False)

        return HttpResponse(geojson, mimetype="application/json", status=200)


class JobFeaturesJSON(ListView):
    model = Job
    show_detailed_properties = False

    def get(self, request, *args, **kwargs):
        job = get_object_or_404(Job, pk=self.kwargs.get('pk'))
        features_json = json.dumps([f.json_item(self.show_detailed_properties) for f in job.feature_set.all()], indent=2)

        return HttpResponse(features_json, mimetype="application/json", status=200)


class GridGeoJSON(ListView):
    model = Job

    def get(self, request, *args, **kwargs):
        job = get_object_or_404(Job, pk=self.kwargs.get('pk'))
        geojson = job.grid_geoJSON()

        return HttpResponse(geojson, mimetype="application/json", status=200)