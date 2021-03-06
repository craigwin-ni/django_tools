import django.contrib.gis.db.models
import django.db.models
import datetime

class Export(django.contrib.gis.db.models.Model):
    objects = django.contrib.gis.db.models.GeoManager()

    slug = django.db.models.SlugField(null=False, blank=True)
    name = django.db.models.CharField(max_length=128, null=False, blank=False)
    description = django.db.models.TextField(null=True, blank=True)

    query = django.db.models.TextField(null=False, blank=False)
    lastid = django.db.models.IntegerField(null=True, blank=True)
    tableid = django.db.models.CharField(max_length=1024, null=False, blank=False)

    clear = django.db.models.BooleanField(default=False, blank=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super(Export, self).save(*args, **kwargs)

    def __unicode__(self):
        return self.name
