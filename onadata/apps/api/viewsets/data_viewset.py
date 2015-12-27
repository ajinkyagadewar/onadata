import types

from django.db.models import Q
from django.db.utils import DataError
from django.http import Http404
from django.utils import six
from django.utils.translation import ugettext as _
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from rest_framework import status
from rest_framework.decorators import detail_route
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet
from rest_framework.exceptions import ParseError
from rest_framework.settings import api_settings

from onadata.libs.utils.api_export_tools import custom_response_handler
from onadata.apps.api.tools import add_tags_to_instance
from onadata.apps.logger.models.attachment import Attachment
from onadata.apps.logger.models import OsmData
from onadata.apps.logger.models.xform import XForm
from onadata.apps.logger.models.instance import Instance
from onadata.apps.viewer.models.parsed_instance import (
    get_where_clause, query_data)
from onadata.libs.renderers import renderers
from onadata.libs.mixins.anonymous_user_public_forms_mixin import (
    AnonymousUserPublicFormsMixin)
from onadata.libs.mixins.authenticate_header_mixin import \
    AuthenticateHeaderMixin
from onadata.libs.mixins.cache_control_mixin import CacheControlMixin
from onadata.libs.mixins.etags_mixin import ETagsMixin
from onadata.libs.mixins.total_header_mixin import TotalHeaderMixin
from onadata.apps.api.permissions import XFormPermissions
from onadata.libs.serializers.data_serializer import DataSerializer
from onadata.libs.serializers.data_serializer import (
    DataInstanceSerializer,
    InstanceHistorySerializer)
from onadata.libs.serializers.data_serializer import JsonDataSerializer
from onadata.libs.serializers.data_serializer import OSMSerializer
from onadata.libs.serializers.geojson_serializer import GeoJsonSerializer
from onadata.libs import filters
from onadata.libs.permissions import CAN_DELETE_SUBMISSION
from onadata.libs.utils.viewer_tools import (
    EnketoError,
    get_enketo_edit_url)
from onadata.libs.data import parse_int
from onadata.apps.api.permissions import ConnectViewsetPermissions
from onadata.apps.api.tools import get_baseviewset_class
from onadata.libs.mixins.profiler_mixin import ProfilerMixin
from onadata.libs.utils.profiler import profile

SAFE_METHODS = ['GET', 'HEAD', 'OPTIONS']
BaseViewset = get_baseviewset_class()


def get_data_and_form(kwargs):
    data_id = str(kwargs.get('dataid'))
    if not data_id.isdigit():
        raise ParseError(_(u"Data ID should be an integer"))

    return (data_id, kwargs.get('format'))


class DataViewSet(AnonymousUserPublicFormsMixin,
                  AuthenticateHeaderMixin,
                  ETagsMixin, CacheControlMixin,
                  TotalHeaderMixin, ProfilerMixin,
                  BaseViewset,
                  ModelViewSet):
    """
    This endpoint provides access to submitted data.
    """

    renderer_classes = api_settings.DEFAULT_RENDERER_CLASSES + [
        renderers.XLSRenderer,
        renderers.XLSXRenderer,
        renderers.CSVRenderer,
        renderers.CSVZIPRenderer,
        renderers.SAVZIPRenderer,
        renderers.SurveyRenderer,
        renderers.GeoJsonRenderer,
        renderers.KMLRenderer,
        renderers.OSMRenderer,
    ]

    filter_backends = (filters.AnonDjangoObjectPermissionFilter,
                       filters.XFormOwnerFilter,
                       filters.DataFilter)
    serializer_class = DataSerializer
    permission_classes = (XFormPermissions,)
    lookup_field = 'pk'
    lookup_fields = ('pk', 'dataid')
    extra_lookup_fields = None
    public_data_endpoint = 'public'
    paginate_by = 1000000
    paginate_by_param = 'page_size'
    page_kwarg = 'page'

    queryset = XForm.objects.filter()

    def get_serializer_class(self):
        pk_lookup, dataid_lookup = self.lookup_fields
        pk = self.kwargs.get(pk_lookup)
        dataid = self.kwargs.get(dataid_lookup)
        fmt = self.kwargs.get('format', self.request.GET.get("format"))
        sort = self.request.GET.get("sort")
        fields = self.request.GET.get("fields")
        if fmt == Attachment.OSM:
            serializer_class = OSMSerializer
        elif fmt == 'geojson':
            serializer_class = GeoJsonSerializer
        elif pk is not None and dataid is None \
                and pk != self.public_data_endpoint:
            if sort or fields:
                serializer_class = JsonDataSerializer
            else:
                serializer_class = DataInstanceSerializer
        else:
            serializer_class = \
                super(DataViewSet, self).get_serializer_class()

        return serializer_class

    def get_object(self, queryset=None):
        obj = super(DataViewSet, self).get_object()
        pk_lookup, dataid_lookup = self.lookup_fields
        pk = self.kwargs.get(pk_lookup)
        dataid = self.kwargs.get(dataid_lookup)

        if pk is not None and dataid is not None:
            try:
                int(dataid)
            except ValueError:
                raise ParseError(_(u"Invalid dataid %(dataid)s"
                                   % {'dataid': dataid}))

            obj = get_object_or_404(Instance, pk=dataid, xform__pk=pk)

        return obj

    def _get_public_forms_queryset(self):
        return XForm.objects.filter(Q(shared=True) | Q(shared_data=True))

    def _filtered_or_shared_qs(self, qs, pk):
        filter_kwargs = {self.lookup_field: pk}
        qs = qs.filter(**filter_kwargs)

        if not qs:
            filter_kwargs['shared_data'] = True
            qs = XForm.objects.filter(**filter_kwargs)

            if not qs:
                raise Http404(_(u"No data matches with given query."))

        return qs

    def filter_queryset(self, queryset, view=None):
        qs = super(DataViewSet, self).filter_queryset(queryset)
        pk = self.kwargs.get(self.lookup_field)

        if pk:
            try:
                int(pk)
            except ValueError:
                if pk == self.public_data_endpoint:
                    qs = self._get_public_forms_queryset()
                else:
                    raise ParseError(_(u"Invalid pk %(pk)s" % {'pk': pk}))
            else:
                qs = self._filtered_or_shared_qs(qs, pk)
        else:
            tags = self.request.query_params.get('tags')
            not_tagged = self.request.query_params.get('not_tagged')

            if tags and isinstance(tags, six.string_types):
                tags = tags.split(',')
                qs = qs.filter(tags__name__in=tags)
            if not_tagged and isinstance(not_tagged, six.string_types):
                not_tagged = not_tagged.split(',')
                qs = qs.exclude(tags__name__in=not_tagged)

        return qs

    @detail_route(methods=['GET', 'POST', 'DELETE'],
                  extra_lookup_fields=['label', ])
    def labels(self, request, *args, **kwargs):
        http_status = status.HTTP_400_BAD_REQUEST
        self.object = instance = self.get_object()

        if request.method == 'POST':
            add_tags_to_instance(request, instance)
            http_status = status.HTTP_201_CREATED

        tags = instance.tags
        label = kwargs.get('label')

        if request.method == 'GET' and label:
            data = [tag['name'] for tag in
                    tags.filter(name=label).values('name')]

        elif request.method == 'DELETE' and label:
            count = tags.count()
            tags.remove(label)

            # Accepted, label does not exist hence nothing removed
            http_status = status.HTTP_200_OK if count > tags.count() \
                else status.HTTP_404_NOT_FOUND

            data = list(tags.names())
        else:
            data = list(tags.names())

        if request.method == 'GET':
            http_status = status.HTTP_200_OK

        self.etag_data = data

        return Response(data, status=http_status)

    @detail_route(methods=['GET'])
    def enketo(self, request, *args, **kwargs):
        self.object = self.get_object()
        data = {}
        if isinstance(self.object, XForm):
            raise ParseError(_(u"Data id not provided."))
        elif(isinstance(self.object, Instance)):
            if request.user.has_perm("change_xform", self.object.xform):
                return_url = request.query_params.get('return_url')
                if not return_url:
                    raise ParseError(_(u"return_url not provided."))

                try:
                    data["url"] = get_enketo_edit_url(
                        request, self.object, return_url)
                except EnketoError as e:
                    data['detail'] = "{}".format(e)
            else:
                raise PermissionDenied(_(u"You do not have edit permissions."))

        self.etag_data = data

        return Response(data=data)

    def destroy(self, request, *args, **kwargs):
        self.object = self.get_object()

        if isinstance(self.object, XForm):
            raise ParseError(_(u"Data id not provided."))
        elif isinstance(self.object, Instance):

            if request.user.has_perm(
                    CAN_DELETE_SUBMISSION, self.object.xform):
                self.object.set_deleted(timezone.now())
            else:
                raise PermissionDenied(_(u"You do not have delete "
                                         u"permissions."))

        return Response(status=status.HTTP_204_NO_CONTENT)

    def retrieve(self, request, *args, **kwargs):
        data_id, _format = get_data_and_form(kwargs)
        self.object = instance = self.get_object()

        if _format == 'json' or _format is None or _format == 'debug':
            return Response(instance.json)
        elif _format == 'xml':
            return Response(instance.xml)
        elif _format == 'geojson':
            return super(DataViewSet, self)\
                .retrieve(request, *args, **kwargs)
        elif _format == Attachment.OSM:
            serializer = self.get_serializer(instance.osm_data.all())

            return Response(serializer.data)
        else:
            raise ParseError(
                _(u"'%(_format)s' format unknown or not implemented!" %
                  {'_format': _format}))

    @detail_route(methods=['GET'])
    def history(self, request, *args, **kwargs):
        data_id, _format = get_data_and_form(kwargs)
        instance = self.get_object()

        # retrieve all history objects and return them
        if _format == 'json' or _format is None or _format == 'debug':
            instance_history = instance.submission_history.all()
            serializer = InstanceHistorySerializer(
                instance_history, many=True)
            return Response(serializer.data)
        else:
            raise ParseError(
                _(u"'%(_format)s' format unknown or not implemented!" %
                  {'_format': _format}))

    @profile("get_data.prof")
    def list(self, request, *args, **kwargs):
        fields = request.GET.get("fields")
        query = request.GET.get("query", {})
        sort = request.GET.get("sort")
        start = parse_int(request.GET.get("start"))
        limit = parse_int(request.GET.get("limit"))
        export_type = kwargs.get('format', request.GET.get("format"))
        lookup_field = self.lookup_field
        lookup = self.kwargs.get(lookup_field)
        is_public_request = lookup == self.public_data_endpoint

        if lookup_field not in kwargs.keys():
            self.object_list = self.filter_queryset(self.get_queryset())
            serializer = self.get_serializer(self.object_list, many=True)

            return Response(serializer.data)

        if is_public_request:
            self.object_list = self._get_public_forms_queryset()
        elif lookup:
            qs = self.filter_queryset(self.get_queryset())
            self.object_list = Instance.objects.filter(xform__in=qs,
                                                       deleted_at=None)
            tags = self.request.query_params.get('tags')
            not_tagged = self.request.query_params.get('not_tagged')

            if tags and isinstance(tags, six.string_types):
                tags = tags.split(',')
                self.object_list = self.object_list.filter(tags__name__in=tags)
            if not_tagged and isinstance(not_tagged, six.string_types):
                not_tagged = not_tagged.split(',')
                self.object_list = \
                    self.object_list.exclude(tags__name__in=not_tagged)

        if (export_type is None or export_type in ['json', 'jsonp', 'debug']) \
                and hasattr(self, 'object_list'):
            return self._get_data(query, fields, sort, start, limit,
                                  is_public_request)

        xform = self.get_object()

        if export_type == Attachment.OSM:
            osm_list = OsmData.objects.filter(instance__xform=xform)
            page = self.paginate_queryset(osm_list)
            serializer = self.get_serializer(page)

            return Response(serializer.data)

        elif export_type is None or export_type in ['json']:
            # perform default viewset retrieve, no data export
            return super(DataViewSet, self).list(request, *args, **kwargs)

        elif export_type == 'geojson':
            serializer = self.get_serializer(self.object_list, many=True)

            return Response(serializer.data)

        return custom_response_handler(request, xform, query, export_type)

    def _get_data(self, query, fields, sort, start, limit, is_public_request):
        try:
            where, where_params = get_where_clause(query)

            if where:
                self.object_list = self.object_list.extra(where=where,
                                                          params=where_params)
            self.total_count = self.object_list.count()

            if (start and limit or limit) and (not sort and not fields):
                start = start if start is not None else 0
                limit = limit if start is None or start == 0 else start + limit
                self.object_list = \
                    self.object_list.order_by('pk')[start: limit]
            elif (sort or limit or start or fields) and not is_public_request:
                if self.object_list.count():
                    xform = self.object_list[0].xform
                    self.object_list = \
                        query_data(xform, query=query, sort=sort,
                                   start_index=start, limit=limit,
                                   fields=fields)
        except ValueError, e:
            raise ParseError(unicode(e))
        except DataError, e:
            raise ParseError(unicode(e))

        if not isinstance(self.object_list, types.GeneratorType):
            page = self.paginate_queryset(self.object_list)
            serializer = self.get_serializer(page)
        else:
            serializer = self.get_serializer(self.object_list, many=True)
            page = None

        return Response(serializer.data)


class AuthenticatedDataViewSet(DataViewSet):
    permission_classes = (ConnectViewsetPermissions,)
