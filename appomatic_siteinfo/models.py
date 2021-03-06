import django.contrib.gis.db.models
import django.db.models
import django.core.urlresolvers
import django.forms
import django.contrib.gis.geos
import django.contrib.gis.measure
import django.contrib.auth.models
import ckeditor.fields
import fcdjangoutils.geomodels
import appomatic_renderable.models
import appomatic_mapserver.maptemplates
import appomatic_mapserver.models
import datetime
from django.conf import settings
import pytz
import re
import uuid
import urllib
import math
import csv
import StringIO
import shapely

class Source(appomatic_renderable.models.Source):
    import_id = django.db.models.IntegerField(null=True, blank=True, default=-1)

    @classmethod
    def get(cls, tool, argument):
        sources = cls.objects.filter(tool=tool, argument=argument)
        if sources:
            return sources[0]
        else:
            source = cls(tool=tool, argument=argument)
            source.save()
            return source

class BaseModel(django.contrib.gis.db.models.Model, appomatic_renderable.models.Renderable, fcdjangoutils.modelhelpers.SubclasModelMixin):
    objects = django.contrib.gis.db.models.GeoManager()

    GUID_FIELDS = ['type']
    GUID_BASEURL = 'http://siteinfo.skytruth.org/'
    
    src = django.db.models.ForeignKey(Source, blank=True, null=True, related_name="objects_from_this_source")
    import_id = django.db.models.IntegerField(null=True, blank=True, db_index=True, verbose_name="Importing id")
    quality = django.db.models.FloatField(default = 1.0, db_index=True, verbose_name="Quality of data")
    guuid = django.db.models.CharField(max_length=64, null=False, blank=True, db_index=True)
    info = fcdjangoutils.fields.JsonField(null=True, blank=True)
    typename = django.db.models.CharField(max_length=256, null=True, blank=True, db_index=True)

    def save(self):
        t = type(self.leafclassobject)
        self.typename = "%s.%s" % (t.__model__, t.__name__)
        super(BaseModel, self).save()

    @classmethod
    def get_or_create(cls, src, import_id, **kw):
        items = cls.objects.filter(src=src, import_id=import_id)
        if items: return items[0]
        item = cls(src=src, import_id=import_id, **kw)
        item.save()
        return item

    @classmethod
    def get(cls, *arg, **kw):
        return cls.get_or_create(*arg, **kw)

    @fcdjangoutils.modelhelpers.subclassproxy
    def generate_guuid(self):
        fields = list(self.leafclassobject.GUID_FIELDS)
        fields.sort()
        
        def fieldvalue(value):
            if isinstance(value, django.db.models.Model):
                # value.save() # Only needed when fixing old data w/o guuid:s
                return value.guuid
            return urllib.quote_plus(unicode(value).encode("utf-8"))

        return str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                self.GUID_BASEURL + '/'.join((urllib.quote_plus(unicode(getattr(self, field)).encode("utf-8"))
                                              for field in fields))))

    def save(self, *arg, **kw):
        self.guuid = self.generate_guuid()
        super(BaseModel, self).save(*arg, **kw)

    def get_absolute_url(self):
        return django.core.urlresolvers.reverse('appomatic_siteinfo.views.basemodel', kwargs={'guuid': self.guuid})
    
    @fcdjangoutils.modelhelpers.subclassproxy
    def render__csv(self, request, context):
        s = StringIO.StringIO()
        c = csv.writer(s)

        for field in self._meta.fields:
            if field.name.endswith("_ptr"): continue
            value = getattr(self, field.name)
            if value is None or type(value).__name__ == 'RelatedManager': continue
            value = getattr(self, field.name)
            def writevalue(path, value):
                if isinstance(value, dict):
                    for key, subvalue in value.iteritems():
                        writevalue(path + [key], subvalue)
                else:
                    c.writerow(['.'.join(path), value])

            writevalue([field.name], getattr(self, field.name))

        for name in self._meta.get_all_field_names():
            if field.name.endswith("_ptr"): continue
            value = getattr(self, name)
            if value is None or type(value).__name__ != 'RelatedManager' or value.count() == 0: continue
            c.writerow([])
            c.writerow(['Field:', name])
            rows = value.all()

            cols = [field.name for field in rows[0]._meta.fields
                    if not field.name.endswith("_ptr") and type(getattr(rows[0], field.name)) != 'RelatedManager']
            cols.sort()
            c.writerow(cols)

            for row in rows:
                c.writerow([getattr(row, col) for col in cols])

        res = django.http.HttpResponse(s.getvalue(), content_type="text/csv")
        res['Content-Disposition'] = 'attachment; filename="%s.csv"' % (self.guuid,)
        return res

    def merge(self, other):
        info = other.info or {}
        info.update(self.info or {})
        other.info = info
    
        for name in dir(type(self)):
            if name.startswith("__"): continue
            field = getattr(type(self), name, None)
            fieldtype = type(field)
            if fieldtype is django.db.models.fields.related.ForeignRelatedObjectsDescriptor:
                relatedname = field.related.field.name
                for item in getattr(self, name).all():
                    setattr(item, relatedname, other)
                    item.save()
            elif fieldtype is django.db.models.fields.related.ReverseManyRelatedObjectsDescriptor:
                for item in getattr(self, name).all():
                    getattr(other, name).add(item)
                getattr(self, name).clear()
        
        other.save()
        self.delete()
        return other

class LocationData(BaseModel):
    objects = django.contrib.gis.db.models.GeoManager()
    latitude = django.db.models.FloatField(null=True, blank=True, db_index=True, verbose_name="Latitude")
    longitude = django.db.models.FloatField(null=True, blank=True, db_index=True, verbose_name="Longitude")
    location = django.contrib.gis.db.models.GeometryField(null=True, blank=True, db_index=True)

    GUID_FIELDS = BaseModel.GUID_FIELDS + ["latitude", "longitude"]
    
    def set_location(self, latitude, longitude):
        self.latitude = latitude
        self.longitude = longitude

    def update_location(self, latitude, longitude):
        if latitude is not None and longitude is not None and (self.latitude is None or self.longitude is None):
            self.set_location(latitude, longitude)
            self.save()

    def save(self, *arg, **kw):
        if self.longitude is not None and self.latitude is not None:
            self.location = django.contrib.gis.geos.Point(self.longitude, self.latitude)
        else:
            self.location = None

        super(LocationData, self).save(*arg, **kw)

class Aliased(object):
    AliasClass = NotImplemented

    @classmethod
    def get_or_create(cls, name, **kw):
        self = cls(name = name, **kw)
        self.save()
        return self

    @classmethod
    def get(cls, name, *arg, **kw):
        if not name: return None
        name = name.strip()
        lookup_name = re.sub(r'[^a-zA-Z0-9][^a-zA-Z0-9]*', '%', name.lower())
        aliases = cls.AliasClass.objects.all().extra(where=["name ilike %s"], params=[lookup_name])
        if aliases:
            return aliases[0].alias_for
        self = cls.get_or_create(name, *arg, **kw)
        alias = cls.AliasClass(alias_for = self, name = name)
        alias.save()
        return self

    def merge(self, other):
        for alias in self.aliases.all():
            if other.aliases.filter(name = alias.name).count():
                alias.delete()
        super(Aliased, self).merge(other)

class Company(Aliased, BaseModel):
    name = django.db.models.CharField(max_length=256, null=False, blank=False, db_index=True)

    GUID_FIELDS = BaseModel.GUID_FIELDS + ["name"]

    def __unicode__(self):
        return self.name

    @classmethod
    def search(cls, query):
        return cls.objects.filter(aliases__name__icontains=query)


class CompanyAlias(BaseModel):
    name = django.db.models.CharField(max_length=256, null=False, blank=False, db_index=True)
    alias_for = django.db.models.ForeignKey(Company, related_name="aliases")

    GUID_FIELDS = BaseModel.GUID_FIELDS + ["alias_for", "name"]

    def __unicode__(self):
        return "%s: %s" % (self.alias_for.name, self.name)
Company.AliasClass = CompanyAlias

class Site(Aliased, LocationData):
    objects = django.contrib.gis.db.models.GeoManager()

    name = django.db.models.CharField(max_length=256, null=False, blank=False, db_index=True)
    datetime = django.db.models.DateTimeField(null=True, blank=True, db_index=True)

    operators = django.db.models.ManyToManyField(Company, related_name='operates_at_sites')
    suppliers = django.db.models.ManyToManyField(Company, related_name="supplied_sites")
    chemicals = django.db.models.ManyToManyField("Chemical", related_name="used_at_sites")

    # info = fcdjangoutils.fields.JsonField(null=True, blank=True)

    @classmethod
    def get_or_create(cls, name, latitude, longitude):
        if longitude is not None and latitude is not None:
            location = django.contrib.gis.geos.Point(longitude, latitude)
            sites = cls.objects.filter(location__distance_lt=(location, django.contrib.gis.measure.Distance(m=100)))
        else:
            sites = []
        if sites:
            site = sites[0]
        else:
            site = Site(name = name, latitude=latitude, longitude=longitude)
            site.save()
        return site

    @classmethod
    def get(cls, name, latitude=None, longitude=None):
        if not name: return None
        name = name.strip()
        self = cls.get_or_create(name, latitude=latitude, longitude=longitude)

        lookup_name = re.sub(r'[^a-zA-Z0-9][^a-zA-Z0-9]*', '%', name.strip().lower())
        aliases = self.aliases.all().extra(where=["name ilike %s"], params=[lookup_name])
        if not aliases:
            alias = cls.AliasClass(alias_for = self, name = name)
            alias.save()
        if latitude is not None and longitude is not None:
            self.update_location(latitude, longitude)
        return self

    def merge(self, other = None):
        if other is None:
            latitude = self.latitude
            longitude = self.longitude
            self.latitude = None
            self.longitude = None
            self.location = None
            self.save()
            other = Site.get(self.name, latitude, longitude)

        if self.datetime is not None and (other.datetime is None or self.datetime > other.datetime):
            other.datetime = self.datetime

        super(Site, self).merge(other)

    def __unicode__(self):
        return self.name

    def handle__read(self, request, style):
        return {'comment_form': CommentForm()}

    def handle__comment(self, request, style):
        form = CommentForm(request.POST)
        if form.is_valid():
            comment = form.save(commit=False)
            comment.site = self
            if request.user.is_authenticated():
                comment.author = request.user
            comment.save()
            form = CommentForm()
        return {'comment_form': form}

    @classmethod
    def search(cls, query):
        return cls.objects.filter(name__icontains=query)

    def get_viirs_data(self):
        import appomatic_mapdata.models
        import django.contrib.gis.measure
        return appomatic_mapdata.models.Viirs.objects.filter(location__distance_lte=(self.location, django.contrib.gis.measure.D(m=750)))

class SiteAlias(BaseModel):
    name = django.db.models.CharField(max_length=256, null=False, blank=False, db_index=True)
    alias_for = django.db.models.ForeignKey(Site, related_name="aliases")

    GUID_FIELDS = BaseModel.GUID_FIELDS + ["alias_for", "name"]

    def __unicode__(self):
        return "%s: %s" % (self.alias_for.name, self.name)
Site.AliasClass = SiteAlias

class Well(LocationData):
    objects = django.contrib.gis.db.models.GeoManager()

    api = django.db.models.CharField(max_length=128, null=False, blank=False, db_index=True)
    site = django.db.models.ForeignKey(Site, related_name="wells")
    datetime = django.db.models.DateTimeField(null=True, blank=True, db_index=True)

    operators = django.db.models.ManyToManyField(Company, related_name='operates_at_wells')
    suppliers = django.db.models.ManyToManyField(Company, related_name="supplied_wells")
    chemicals = django.db.models.ManyToManyField("Chemical", related_name="used_at_wells")

    well_type = django.db.models.CharField(max_length=128, null=True, blank=True, db_index=True)

    info = fcdjangoutils.fields.JsonField(null=True, blank=True)

    GUID_FIELDS = LocationData.GUID_FIELDS + ["api"]

    def update_location(self, latitude, longitude):
        LocationData.update_location(self, latitude, longitude)
        self.site.update_location(latitude, longitude)

    @classmethod
    def get(cls, api, site_name=None, latitude=None, longitude=None, conventional=True):
        wells = cls.objects.filter(api=api)
        if wells:
            well = wells[0]
        else:
            site = Site.get(site_name or api, latitude, longitude)
            well = Well(
                api=api,
                site=site,
                info = {'conventional':conventional})
            well.save()
        if latitude is not None and longitude is not None:
            well.update_location(latitude, longitude)
        return well

    def __unicode__(self):
        return "%s: %s" % (self.site, self.api)

    def handle__read(self, request, style):
        return {'comment_form': CommentForm()}

    def handle__comment(self, request, style):
        form = CommentForm(request.POST)
        if form.is_valid():
            comment = form.save(commit=False)
            comment.site = self.site
            comment.well = self
            if request.user.is_authenticated():
                comment.author = request.user
            comment.save()
            form = CommentForm()
        return {'comment_form': form}

    @classmethod
    def search(cls, query):
        return cls.objects.filter(api__icontains=query)


class ChemicalPurpose(Aliased, BaseModel):
    objects = django.contrib.gis.db.models.GeoManager()
    name = django.db.models.CharField(max_length=256, db_index=True)

    GUID_FIELDS = BaseModel.GUID_FIELDS + ["name"]

    def __unicode__(self):
        return self.name

class ChemicalPurposeAlias(BaseModel):
    name = django.db.models.CharField(max_length=256, null=False, blank=False, db_index=True)
    alias_for = django.db.models.ForeignKey(ChemicalPurpose, related_name="aliases")

    GUID_FIELDS = BaseModel.GUID_FIELDS + ["alias_for", "name"]

    def __unicode__(self):
        return "%s: %s" % (self.alias_for.name, self.name)
ChemicalPurpose.AliasClass = ChemicalPurposeAlias


class Chemical(Aliased, BaseModel):
    objects = django.contrib.gis.db.models.GeoManager()
    
    name = django.db.models.CharField(max_length=256, db_index=True)
    trade_name = django.db.models.CharField(max_length=256, null=True, blank=True, db_index=True)
    ingredients = django.db.models.CharField(max_length=256, null=True, blank=True, db_index=True)
    cas_type = django.db.models.CharField(max_length=32, null=True, blank=True, db_index=True)
    cas_number = django.db.models.CharField(max_length=64, null=True, blank=True, db_index=True)
    suppliers = django.db.models.ManyToManyField(Company, related_name="supplies")
    purposes = django.db.models.ManyToManyField(ChemicalPurpose, related_name="chemicals")
    comments = django.db.models.TextField(null=True, blank=True)

    GUID_FIELDS = BaseModel.GUID_FIELDS + ["trade_name", "ingredients", "cas_type", "cas_number"]

    @classmethod
    def get(cls, trade_name, ingredients, cas_type, cas_number, comments):
        return super(Chemical, cls).get(
            name=trade_name or ingredients or cas_number or cas_type or comments,
            trade_name=trade_name,
            ingredients=ingredients,
            cas_type=cas_type,
            cas_number=cas_number,
            comments=comments)
    
    def __unicode__(self):
        return self.name


    @classmethod
    def search(cls, query):
        return cls.objects.filter(django.db.models.Q(aliases__name__icontains=query)
                                  |django.db.models.Q(purposes__aliases__name__icontains=query))

class ChemicalAlias(BaseModel):
    name = django.db.models.CharField(max_length=256, null=False, blank=False, db_index=True)
    alias_for = django.db.models.ForeignKey(Chemical, related_name="aliases")

    GUID_FIELDS = BaseModel.GUID_FIELDS + ["alias_for", "name"]

    def __unicode__(self):
        return "%s: %s" % (self.alias_for.name, self.name)
Chemical.AliasClass = ChemicalAlias


# Event types

class Event(LocationData):
    objects = django.contrib.gis.db.models.GeoManager()

    datetime = django.db.models.DateTimeField(null=False, blank=False, db_index=True, default=lambda:datetime.datetime.now(pytz.utc), verbose_name="Date/time of event")

    GUID_FIELDS = LocationData.GUID_FIELDS + ["datetime"]

    def __unicode__(self):
        return "%s" % (self.datetime,)

    class Meta:
        ordering = ('-datetime', )

class SiteEvent(Event):
    objects = django.contrib.gis.db.models.GeoManager()

    site = django.db.models.ForeignKey(Site, related_name="events")
    well = django.db.models.ForeignKey(Well, blank=True, null=True, related_name="events")

    GUID_FIELDS = Event.GUID_FIELDS + ["site", "well"]

    def save(self, *arg, **kw):
        if self.latitude is None or self.longitude is None:
            if self.well:
                self.longitude = self.well.longitude
                self.latitude = self.well.latitude
            else:
                self.longitude = self.site.longitude
                self.latitude = self.site.latitude
        Event.save(self, *arg, **kw)
        if not self.site.datetime or self.site.datetime < self.datetime:
            self.site.datetime = self.datetime
            self.site.save()
        if self.well and (not self.well.datetime or self.well.datetime < self.datetime):
            self.well.datetime = self.datetime
            self.well.save()

    def __unicode__(self):
        return "%s @ %s" % (self.datetime, self.well or self.site)

class OperatorEvent(SiteEvent):
    objects = django.contrib.gis.db.models.GeoManager()

    operator = django.db.models.ForeignKey(Company, related_name="events", null=True, blank=True)

    def save(self, *arg, **kw):
        SiteEvent.save(self, *arg, **kw)
        if self.operator:
            self.site.operators.add(self.operator)
            if self.well:
                self.well.operators.add(self.operator)

    def __unicode__(self):
        return "%s @ %s for %s" % (self.datetime, self.well or self.site, self.operator)


class OperatorInfoEvent(OperatorEvent):
    objects = django.contrib.gis.db.models.GeoManager()

    infourl = django.db.models.TextField(null=True, blank=True)
    # info = fcdjangoutils.fields.JsonField(null=True, blank=True)

class PermitEvent(OperatorInfoEvent):
    objects = django.contrib.gis.db.models.GeoManager()

class SpudEvent(OperatorInfoEvent):
    objects = django.contrib.gis.db.models.GeoManager()

class InspectionEvent(OperatorInfoEvent):
    objects = django.contrib.gis.db.models.GeoManager()

class ViolationEvent(OperatorInfoEvent):
    objects = django.contrib.gis.db.models.GeoManager()

class PollutionEvent(OperatorInfoEvent):
    objects = django.contrib.gis.db.models.GeoManager()

class UserEvent(SiteEvent):
    objects = django.contrib.gis.db.models.GeoManager()

    author = django.db.models.ForeignKey(django.contrib.auth.models.User, related_name="events", blank=True, null=True)

    def __unicode__(self):
        return "%s @ %s for %s" % (self.datetime, self.well or self.site, self.author)

class CommentEvent(UserEvent):
    objects = django.contrib.gis.db.models.GeoManager()

    content = ckeditor.fields.RichTextField(verbose_name="Comment", config_name='small')

class ChemicalUsageEvent(OperatorInfoEvent):
    objects = django.contrib.gis.db.models.GeoManager()

class ChemicalUsageEventChemical(BaseModel):
    objects = django.contrib.gis.db.models.GeoManager()
    
    event = django.db.models.ForeignKey(ChemicalUsageEvent, related_name="chemicals")
    
    chemical = django.db.models.ForeignKey(Chemical, related_name="used_in_events")
    supplier = django.db.models.ForeignKey(Company, null=True, blank=True, related_name="supplied_event")
    purpose = django.db.models.ForeignKey(ChemicalPurpose, null=True, blank=True, related_name="events")
    additive_concentration = django.db.models.FloatField(null=True, blank=True)
    weight = django.db.models.FloatField(null=True, blank=True)
    ingredient_weight = django.db.models.FloatField(null=True, blank=True)
    hf_fluid_concentration = django.db.models.FloatField(null=True, blank=True)
    
    # info = fcdjangoutils.fields.JsonField(null=True, blank=True)

    def save(self, *arg, **kw):
        BaseModel.save(self, *arg, **kw)
        self.event.site.chemicals.add(self.chemical)
        if self.event.well:
            self.event.well.chemicals.add(self.chemical)
        if self.purpose:
            self.chemical.purposes.add(self.purpose)
        if self.supplier:
            self.chemical.suppliers.add(self.supplier)
            self.event.site.suppliers.add(self.supplier)
            if self.event.well:
                self.event.well.suppliers.add(self.supplier)

    def __unicode__(self):
        return "%s used in %s" % (self.chemical, self.event)

    class Meta:
        ordering = ('-event__datetime', )

class FracEvent(ChemicalUsageEvent):
    objects = django.contrib.gis.db.models.GeoManager()
    true_vertical_depth = django.db.models.FloatField(null=True, blank=True)
    total_water_volume = django.db.models.FloatField(null=True, blank=True)
    published = django.db.models.DateTimeField(null=True, blank=True)


class Status(Aliased, BaseModel):
    objects = django.contrib.gis.db.models.GeoManager()
    name = django.db.models.CharField(max_length=256, db_index=True)

    GUID_FIELDS = BaseModel.GUID_FIELDS + ["name"]

    def __unicode__(self):
        return self.name

class StatusAlias(BaseModel):
    name = django.db.models.CharField(max_length=256, null=False, blank=False, db_index=True)
    alias_for = django.db.models.ForeignKey(Status, related_name="aliases")

    GUID_FIELDS = BaseModel.GUID_FIELDS + ["alias_for", "name"]

    def __unicode__(self):
        return "%s: %s" % (self.alias_for.name, self.name)
Status.AliasClass = StatusAlias


class StatusEvent(SiteEvent):
    objects = django.contrib.gis.db.models.GeoManager()

    status = django.db.models.ForeignKey(Status, related_name="events")
    info = fcdjangoutils.fields.JsonField(null=True, blank=True)


class CommentForm(django.forms.ModelForm):
    class Meta:
        model = CommentEvent
        fields = ['content']

# Map stuff

class SiteInfoMap(appomatic_mapserver.models.BuiltinApplication):
    name = 'SiteInfo'

    @property
    def configuration(self):
        conf = {
            "center": {
                "lat": 40.903133814657984,
                "lon": -79.1784667968776},
            "timemax": "end",
            "zoom": 8,
            "classes": "noeventlist",
            "timemin": "start",
            "options": {
                "protocol": {
                    "params": {
                        "limit": 50
                        }
                    }
                }
            }

        if self.urlquery.get('minimap', False):
            conf['classes'] += " notimeslider"

        center = SelectedSitesLayer(self).query.aggregate(
            latitude = django.db.models.Avg("latitude"),
            longitude = django.db.models.Avg("longitude"))
        if center['latitude'] is not None:
            conf['center']['lat'] = center['latitude']
            conf['center']['lon'] = center['longitude']
            conf['zoom'] = 15

        return conf

    def get_layer(self, urlquery):
        if urlquery['layer'] == 'appomatic_siteinfo.models.AllSitesLayer':
            return AllSitesLayer(self)
        elif urlquery['layer'] == 'appomatic_siteinfo.models.SelectedSitesLayer':
            return SelectedSitesLayer(self)

    def get_layers(self):
        return [AllSitesLayer(self), SelectedSitesLayer(self)]

class AllSitesLayer(appomatic_mapserver.models.BuiltinLayer):
    name="Sites"

    backend_type = "appomatic_mapserver.mapsources.GridSnappingMap"
    template = "appomatic_siteinfo.models.AllSitesTemplate"

    definition = {
        "options": {
            "protocol": {
                "params": {
                    "limit": 50
                    }
                }
            }
        }

    @property
    def query(self):
        return Site.objects.all()

class SelectedSitesLayer(appomatic_mapserver.models.BuiltinLayer):
    name="Selected sites"

    backend_type = "appomatic_mapserver.mapsources.StaticMap"
    template = "appomatic_siteinfo.models.SelectedSitesTemplate"

    @property
    def definition(self):
        return {
            "options": {
                "protocol": {
                    "params": self.application.urlquery
                    }
                }
            }

    @property
    def query(self):
        if 'company' in self.application.urlquery:
            return Site.objects.filter(django.db.models.Q(operators__guuid=self.application.urlquery['company'])
                                       | django.db.models.Q(suppliers__guuid=self.application.urlquery['company']))
        elif 'site' in self.application.urlquery:
            return Well.objects.filter(site__guuid=self.application.urlquery['site'])
        elif 'well' in self.application.urlquery:
            return Well.objects.filter(guuid=self.application.urlquery['well'])
        elif 'chemical' in self.application.urlquery:
            return Site.objects.filter(chemicals__guuid=self.application.urlquery['chemical'])
        elif 'query' in self.application.urlquery:
            return Site.search(self.application.urlquery['query'])
        else:
            return Site.objects.filter(id=None) # Empty list

class AllSitesTemplate(appomatic_mapserver.maptemplates.MapTemplateSimple):
    name = "SiteInfo site"
    
    def row_generate_text(self, row):
        appomatic_mapserver.maptemplates.MapTemplateSimple.row_generate_text(self, row)
        if 'guuid' in row:
            row['url'] = django.core.urlresolvers.reverse('appomatic_siteinfo.views.basemodel', kwargs={'guuid': row['guuid']}) + "?style=iframe.html"
        elif 'count' in row:
            row['timemin'] = self.urlquery['datetime__gte']
            row['timemax'] = self.urlquery['datetime__lte']
            row['url'] = (django.core.urlresolvers.reverse('appomatic_siteinfo.views.clustersitelist') + 
                          "?latmin=%(latmin)s&lonmin=%(lonmin)s&latmax=%(latmax)s&lonmax=%(lonmax)s&timemin=%(timemin)s&timemax=%(timemax)s" % row)
        row['description'] = u"""
            <iframe src="%(url)s" style="width: 100%%; height: 100%%; border: none; padding: 0; margin: -5px;">
        """ % row
          
        itemtype = row.get('itemtype', 'item')
        fillColors = {
            'item': "#0000ff",
            'summary': "#00ff00"
            }
        strokeColors = {
            'item': "#000055",
            'summary': "#005500"
            }

        row['style'] = {
          "graphicName": "circle",
          "fillOpacity": 1.0,
          "fillColor": fillColors[itemtype],
          "strokeOpacity": 1.0,
          "strokeColor": strokeColors[itemtype],
          "strokeWidth": 1,
          "pointRadius": int(6 + math.log(row.get('count', 1), 2)),
          }

class SelectedSitesTemplate(AllSitesTemplate):
    def row_generate_text(self, row):
        AllSitesTemplate.row_generate_text(self, row)
        row['style']['fillColor'] = '#ff0000'
        row['style']['strokeColor'] = '#550000'
