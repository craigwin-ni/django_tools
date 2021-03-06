import datetime
import django.db
import math
from django.conf import settings
import fcdjangoutils.sqlutils
import contextlib
import appomatic_mapserver.models

URL_PATTERN = "http://www.marinetraffic.com/ais/shipdetails.aspx?MMSI=%(mmsi)s"
URL_PATTERN_ITU = "http://www.itu.int/cgi-bin/htsh/mars/ship_search.sh?sh_mmsi=%(mmsi)s"


def dictreader(cur):
    for row in cur:
        yield dict(zip([col[0] for col in cur.description], row))

class MapSource(object):
    implementations = {}

    def __new__(cls, layer, urlquery, *arg, **kw):
        if cls is MapSource:
            return cls.implementations[layer.layerdef.backend_type](layer, urlquery, *arg, **kw)
        else:
            return object.__new__(cls, layer, urlquery, *arg, **kw)

    class __metaclass__(type):
        def __init__(cls, name, bases, members):
            type.__init__(cls, name, bases, members)
            module = members.get('__module__', '__main__')
            if name != "MapSource" or module != "appomatic_mapserver.mapsources":
                MapSource.implementations[module + "." + name] = cls

    def __init__(self, layer, urlquery):
        self.layer = layer
        self.urlquery = urlquery

    def __enter__(self):
        self.cur = django.db.connection.cursor()
        return self

    def __exit__(self, type, value, traceback):
        self.cur.close()

    def get_query(self):
        def converttime(s):
            try:
                return int(datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:S").strftime("%s"))
            except:
                try:
                    return int(datetime.datetime.strptime(s, "%Y-%m-%d").strftime("%s"))
                except Exception, e:
                    return int(float(s))

        datetimemin = converttime(self.urlquery.get('datetime__gte', '0'))
        datetimemax = converttime(self.urlquery.get('datetime__lte', '0'))

        lon1,lat1,lon2,lat2 = [float(coord) for coord in self.urlquery['bbox'].split(",")]
        return {
            "timeminstamp": datetimemin,
            "timemaxstamp": datetimemax,
            "timemin": datetime.datetime.utcfromtimestamp(datetimemin),
            "timemax": datetime.datetime.utcfromtimestamp(datetimemax),
            "lonmin": min(lon1, lon2),
            "latmin": min(lat1, lat2),
            "lonmax": max(lon1, lon2),
            "latmax": max(lat1, lat2)
            }

    def get_table_sql(self):
        query = self.layer.layerdef.query
        if isinstance(query, django.db.models.query.QuerySet):
            sql, params = query.query.sql_with_params()
            sql = sql % tuple('%%(mapsource_%s)s' % idx for idx in xrange(0, len(params)))
            params = dict(
                ('mapsource_%s' % idx, value)
                for idx, value in enumerate(params))
            return ("(%s)" % sql, params)
        elif isinstance(query, (tuple, list)):
            return query
        else:
            return (query, {})

    def get_columns(self):
        sql, params = self.get_table_sql()
        return fcdjangoutils.sqlutils.query_columns(self.cur, sql, params)

    def order_by(self):
        groupings = len([key for key in self.get_columns().iterkeys() if key.startswith("grouping")])
        if not groupings:
            return ["1"]
        return ["a.grouping%s desc" % ind for ind in xrange(0, groupings)]

    def get_bboxsql(self):
        bboxmin = "ST_Point(%(lonmin)s, %(latmin)s)"
        bboxmax = "ST_Point(%(lonmax)s, %(latmax)s)"
        bbox = "st_setsrid(ST_MakeBox2D(" + bboxmin + ", " + bboxmax + "), (4326))"
        bboxdiag = "ST_Distance(" + bboxmin + ", " + bboxmax + ")"
        return {
            "bboxmin": bboxmin,
            "bboxmax": bboxmax,
            "bbox": bbox,
            "bboxdiag": bboxdiag
            }

    def get_map_data(self):
        for row in self.get_map_data_raw():
            if row.get('mmsi', None):
                if not row.get('name', None):
                    row['name'] = row['mmsi']
                if not row.get('url', None):
                    row['url'] = URL_PATTERN % row

                row['itu_url'] = URL_PATTERN_ITU % row

            if not row.get('mmsi', None):
                row['mmsi'] = ''
            if not row.get('name', None):
                row['name'] = ''
            if not row.get('url', None):
                row['url'] = ''
            if not row.get('type', None):
                row['type'] = ''
            if not row.get('length', None):
                row['length'] = ''


            yield row

class TolerancePathMap(MapSource):
    name = 'Simplified path'
    def get_tolerance(self):
        query = self.get_query()
        bboxsql = self.get_bboxsql()

        tolerance = self.urlquery.get('tolerance', '')

        if tolerance == 'none':
            return None
        if tolerance == 'minimal':
            return 2 ** settings.TOLERANCE_BASE_MIN
        else:
            if tolerance == "":
                self.cur.execute("select " + bboxsql['bboxdiag'] + " / 100", query)
                tolerance = self.cur.fetchone()[0]

            # Round to nearest (lower) 2^x as those are the only tolerances implemented in the view...
            # Fixme: Handle min and max...
            return 2**int(math.log(float(tolerance), 2))

    def order_by(self):
        return MapSource.order_by(self) + ["mmsi desc"]

    def get_map_data_raw(self):
        query = self.get_query()
        bboxsql = self.get_bboxsql()

        query['tolerance'] = self.get_tolerance()

        tolerancetest = "tolerance = %(tolerance)s"
        if query['tolerance'] is None:
            tolerancetest = "tolerance is null"

        table_sql, table_query = self.get_table_sql()
        query.update(table_query)

        sql = """
          select
            ST_AsText(shape_binary) as shape,
            extract(epoch from timemin) as datetime,
            timemin as datetime_time,
            *
          from
            (select
               ST_Intersection(
                 ST_locate_between_measures(
                   line,
                   extract(epoch from %(timemin)s::timestamp),
                   extract(epoch from %(timemax)s::timestamp)
                 ),
                 """ + bboxsql['bbox'] + """) as shape_binary,
               ais_path.*
             from
               """ + table_sql + """ as ais_path
             where
               """ + tolerancetest + """
               and not (%(timemax)s < timemin or %(timemin)s > timemax)
               and ST_Intersects(
                 line,
                 """ + bboxsql['bbox'] + """)) as a
          where
            not ST_IsEmpty(shape_binary)
          order by
        """ + ', '.join(self.order_by())

        self.cur.execute(sql, query)
        try:
            for row in dictreader(self.cur):
                yield row
        finally:
            print "TOLERANCE:", query['tolerance']
            print "RESULTS: ", self.cur.rowcount


    def get_timeframe(self):
        table_sql, table_query = self.get_table_sql()

        self.cur.execute("select extract(epoch from min(timemin)) timemin, extract(epoch from max(timemax)) timemax from " + table_sql + " as a", table_query)
        res = dictreader(self.cur).next()
        if res['timemin'] is None: return None
        return res


class EventMap(MapSource):
    name = 'Event list'
    def get_map_data_raw(self):
        query = self.get_query()
        bboxsql = self.get_bboxsql()

        table_sql, table_query = self.get_table_sql()
        query.update(table_query)

        sql = """
          select
            *,
            extract(epoch from datetime) as datetime,
            datetime as datetime_time,
            ST_AsText(location) as shape
          from
            """ + table_sql + """ as a
          where
            not (%(timemax)s < datetime or %(timemin)s > datetime)
            and ST_Contains(
              """ + bboxsql['bbox'] + """, location)
          order by
        """ + ', '.join(self.order_by())

        if 'limit' in self.urlquery:
            sql += "limit %(limit)s"
            query['limit'] = self.urlquery['limit']

        self.cur.execute(sql, query)
        try:
            for row in dictreader(self.cur):
                yield row
        finally:
            print "RESULTS: ", self.cur.rowcount

    def get_timeframe(self):
        table_sql, table_query = self.get_table_sql()

        self.cur.execute("select extract(epoch from min(datetime)) timemin, extract(epoch from max(datetime)) timemax from " + table_sql + " as a", table_query)
        res = dictreader(self.cur).next()
        if res['timemin'] is None: return None
        return res

class GridSnappingEventMap(EventMap):
    name = 'Grid snapping event list'
    def get_map_data_raw(self):
        query = self.get_query()
        bboxsql = self.get_bboxsql()

        table_sql, table_query = self.get_table_sql()
        query.update(table_query)


        query['limit'] = int(self.urlquery.get('limit', 100))
        query['gridsize'] = int(self.urlquery.get('gridsize', 20))
        query['gridsizelon'] = (query['lonmax'] - query['lonmin']) / query['gridsize']
        query['gridsizelat'] = (query['latmax'] - query['latmin']) / query['gridsize']

        sql = """
          select
            'summary' as itemtype,
            avg(extract(epoch from datetime)) as datetime,
            to_timestamp(avg(extract(epoch from datetime))) as datetime_time,
            ST_AsText(ST_Centroid(ST_Union(location))) as shape,
            ST_X(ST_Centroid(ST_Union(location))) as longitude,
            ST_Y(ST_Centroid(ST_Union(location))) as latitude,
            count(*) as count
          from
            """ + table_sql + """ as a
          where
            not (%(timemax)s < datetime or %(timemin)s > datetime)
            and ST_Contains(
              """ + bboxsql['bbox'] + """, location)
          group by
            ST_SnapToGrid(location, %(gridsizelon)s, %(gridsizelat)s)
        """

        with contextlib.closing(django.db.connection.cursor()) as cur2:
            self.cur.execute(sql, query)
            try:
                results = 0
                for row in dictreader(self.cur):
                    if row['count'] > query['limit'] / query['gridsize']:
                        row['lonmin'] = row['longitude'] - query['gridsizelon'] / 2
                        row['lonmax'] = row['longitude'] + query['gridsizelon'] / 2
                        row['latmin'] = row['latitude'] - query['gridsizelat'] / 2
                        row['latmax'] = row['latitude'] + query['gridsizelat'] / 2
                        yield row
                    else:
                        query2 = dict(query)
                        query2['lonmin'] = row['longitude'] - query['gridsizelon'] / 2
                        query2['lonmax'] = row['longitude'] + query['gridsizelon'] / 2
                        query2['latmin'] = row['latitude'] - query['gridsizelat'] / 2
                        query2['latmax'] = row['latitude'] + query['gridsizelat'] / 2

                        sql2 = """
                          select
                            *,
                            extract(epoch from datetime) as datetime,
                            datetime as datetime_time,
                            ST_AsText(location) as shape
                          from
                            """ + table_sql + """ as a
                          where
                            not (%(timemax)s < datetime or %(timemin)s > datetime)
                            and ST_Contains(
                              """ + bboxsql['bbox'] + """, location)
                        """

                        cur2.execute(sql2, query2)
                        try:
                            for row in dictreader(cur2):
                                yield row
                        finally:
                            results += cur2.rowcount
            finally:
                print "GROUPS:", self.cur.rowcount, "SINGLE ITEMS:", results

class GridSnappingMap(MapSource):
    name = 'Grid snapping list with caching'
    def get_map_data_raw(self):
        query = self.get_query()
        if 'timemax' in query: del query['timemax']
        if 'timemin' in query: del query['timemin']
        bboxsql = self.get_bboxsql()

        table_sql, table_query = self.get_table_sql()
        query.update(table_query)

        query['limit'] = int(self.urlquery.get('limit', 100))
        query['gridsize'] = int(self.urlquery.get('gridsize', 10))

        query['snapsize'] = (query['latmax'] - query['latmin']) / query['gridsize']
        query['snaplevel'] = math.ceil(math.log(query['snapsize'], 2))
        query['snapsize'] = math.pow(2, query['snaplevel'])

        caches = appomatic_mapserver.models.GridSnappingMapCache.objects.filter(query=table_query, snaplevel = query['snaplevel'])
        if caches:
            query['cache'] = caches[0].id
        else:
            cache = appomatic_mapserver.models.GridSnappingMapCache(query=table_query, snaplevel = query['snaplevel'])
            cache.save()
            query['cache'] = cache.id
            update_sql = """
              insert into appomatic_mapserver_gridsnappingmapcachedata (cache_id, location, bbox, count)
              select
                %(cache)s,
                ST_Centroid(ST_Union(location)),
                st_setsrid(ST_MakeBox2D(
                  ST_Point(ST_X(ST_SnapToGrid(location, %(snapsize)s, %(snapsize)s)) - (%(snapsize)s / 2),
                           ST_Y(ST_SnapToGrid(location, %(snapsize)s, %(snapsize)s)) - (%(snapsize)s) / 2),
                  ST_Point(ST_X(ST_SnapToGrid(location, %(snapsize)s, %(snapsize)s)) + (%(snapsize)s / 2),
                           ST_Y(ST_SnapToGrid(location, %(snapsize)s, %(snapsize)s)) + (%(snapsize)s / 2))), (4326)),
                count(*)
              from
                """ + table_sql + """ as a
              group by
                ST_SnapToGrid(location, %(snapsize)s, %(snapsize)s)
            """
            update_query = {"snapsize": query['snapsize']}
            update_query.update(query)
            
            print "Updating cache for %s" % (query['snaplevel'],)
            self.cur.execute(update_sql, update_query)
            self.cur.execute("commit")

        sql = """
          select
            'summary' as itemtype,
            500 as datetime,
            to_timestamp(500) as datetime_time,
            ST_AsText(location) as shape,
            ST_X(location) as longitude,
            ST_Y(location) as latitude,
            location,
            bbox,
            count
          from
            appomatic_mapserver_gridsnappingmapcachedata
          where
            cache_id = %(cache)s
            and ST_Contains(
              """ + bboxsql['bbox'] + """, location)
        """

        self.cur.execute(sql + " and count > %(limit)s / %(gridsize)s", query)
        print "GROUPS", self.cur.rowcount
        for row in dictreader(self.cur):
            if row['count'] > query['limit'] / query['gridsize']:
                row['lonmin'] = row['longitude'] - query['snapsize'] / 2
                row['lonmax'] = row['longitude'] + query['snapsize'] / 2
                row['latmin'] = row['latitude'] - query['snapsize'] / 2
                row['latmax'] = row['latitude'] + query['snapsize'] / 2
                yield row

        sql2 = """
          select
            b.*,
            500 as datetime,
            to_timestamp(500) datetime_time,
            ST_AsText(b.location) as shape
          from
            (""" + sql + """ and count <= %(limit)s / %(gridsize)s) as a
            join """ + table_sql + """ as b on
              ST_Contains(a.bbox, b.location)
        """

        print "XXXXXXXXXXXXXXXX"
        print sql2 % query
        self.cur.execute(sql2, query)
        print "SINGLE ITEMS", self.cur.rowcount
        for row in dictreader(self.cur):
            yield row

    def get_timeframe(self):
        return {'timemin': 0, 'timemax': 1000}

class StaticMap(MapSource):
    name = 'Static set of objects'
    def get_map_data_raw(self):
        query = self.get_query()
        bboxsql = self.get_bboxsql()

        table_sql, table_query = self.get_table_sql()
        query.update(table_query)

        sql = """
          select
            *,
            extract(epoch from %(timemin)s :: timestamp) as datetime,
            %(timemin)s :: timestamp as datetime_time,
            ST_AsText(location) as shape
          from
            """ + table_sql + """ as a
          where
            ST_Contains(
              """ + bboxsql['bbox'] + """, location)
          order by
        """ + ', '.join(self.order_by())

        if 'limit' in self.urlquery:
            sql += "limit %(limit)s"
            query['limit'] = self.urlquery['limit']

        self.cur.execute(sql, query)
        try:
            for row in dictreader(self.cur):
                yield row
        finally:
            print "RESULTS: ", self.cur.rowcount

    def get_timeframe(self):
        return None
