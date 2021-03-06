import fcdjangoutils.jsonview
import fcdjangoutils.cors
import django.db
from django.conf import settings
import appomatic_legacymodels.models
import appomatic_siteinfo.models
import appomatic_fracbotserver.models
import datetime
import django.core.exceptions
import pytz
import fracfocustools
import django.http
import django.views.decorators.csrf
import base64
import contextlib
import django.db
import re
import appomatic_siteinfo.management.commands.fracscrapeimport

def set_cookie(response, key, value, max_age = 365 * 24 * 60 * 60):
    expires = datetime.datetime.strftime(
        datetime.datetime.utcnow() + datetime.timedelta(seconds=max_age),
        "%a, %d-%b-%Y %H:%M:%S GMT")
    response.set_cookie(
        key, value, max_age=max_age, expires=expires,
        domain=settings.SESSION_COOKIE_DOMAIN,
        secure=settings.SESSION_COOKIE_SECURE or None)

def track_client(fn):
    def track_client(request, *arg, **kw):
        request.fracbotclient = None
        if 'fracbotclientid' in request.COOKIES:
            clients = appomatic_fracbotserver.models.Client.objects.filter(id = request.COOKIES['fracbotclientid'])
            if clients: request.fracbotclient = clients[0]
        if not request.fracbotclient:
            request.fracbotclient = appomatic_fracbotserver.models.Client(
                info = dict((key, value)
                            for key, value in request.META.iteritems()
                            if isinstance(value, (str, unicode))))
            request.fracbotclient.save()
        response = fn(request, *arg, **kw)
        if 'fracbotclientid' not in request.COOKIES:
            set_cookie(response, 'fracbotclientid', request.fracbotclient.id)
        return response
    return track_client

def log_activity(request, activity_type, **info):
    existing = appomatic_fracbotserver.models.ActivityType.objects.filter(name=activity_type)
    if existing:
        activity_type = existing[0]
    else:
        activity_type = appomatic_fracbotserver.models.ActivityType(name=activity_type)
        activity_type.save()
    appomatic_fracbotserver.models.Activity(client=request.fracbotclient, type=activity_type, info=info).save()


def parseDate(date):
    """Parses dates in american format (mm/dd/yyyy) with optional 0-padding."""
    if not date: return None
    date = date.strip()
    if not date: return None
    month, day, year = [int(x.lstrip("0")) for x in date.split("/")]
    return datetime.date(year, month, day)

def parseFloat(str):
    if not str: return None
    str = str.strip()
    if not str: return None
    return float(str.replace(',', ''))

def get(query):
    if query:
        return query[0]
    return None


def check_record(cur, row):
    api = row["API No."]
    jobdate = row["job_date"] = parseDate(row["Job Start Dt"])
    jobtime = datetime.datetime(jobdate.year, jobdate.month, jobdate.day).replace(tzinfo=pytz.utc)

    cur.execute("""select seqid from "FracFocusScrape" where api = %(api)s and job_date = %(jobdate)s""", {'api': api, 'jobdate':jobdate})
    for dbrow in cur:
        row['pdf_seqid'] = dbrow[0]
    if 'pdf_seqid' not in row:
        cur.execute("""insert into "FracFocusScrape" (api, job_date, state, county, operator, well_name, well_type, latitude, longitude, datum) values(%(API No.)s, %(job_date)s, %(State)s, %(County)s, %(Operator)s, %(WellName)s, NULL,%(Latitude)s, %(Longitude)s, %(Datum)s)""", row)
        cur.execute("select lastval()")
        row['pdf_seqid'] = cur.fetchone()[0]

        cur.execute("""insert into "BotTaskStatus" (task_id, bot, status) values(%(pdf_seqid)s, 'FracFocusReport', 'NEW')""", row)

    cur.execute("""select * from "FracFocusReport" where pdf_seqid = %(pdf_seqid)s""", row)
    for dbrow in cur:
        row['pdf_content'] = dict(zip([dsc[0] for dsc in cur.description], dbrow))

    well = get(appomatic_siteinfo.models.Well.objects.filter(api = api))
    if well:
        row['well_guuid'] = well.guuid
        row['site_guuid'] = well.site.guuid
        event = get(appomatic_siteinfo.models.FracEvent.objects.filter(well = well, datetime = jobtime))
        if event:
            row['event_guuid'] = event.guuid
            row['operator_guuid'] = event.operator.guuid

    if 'operator_guuid' not in row:
        lookup_name = re.sub(r'[^a-zA-Z0-9][^a-zA-Z0-9]*', '%', row['Operator'].lower())
        aliases = appomatic_siteinfo.models.CompanyAlias.objects.all().extra(where=["name ilike %s"], params=[lookup_name])
        if aliases:
            row['operator_guuid'] = aliases[0].alias_for.guuid


@django.views.decorators.csrf.csrf_exempt
@fcdjangoutils.cors.cors
@track_client
@fcdjangoutils.jsonview.json_view
def check_records(request):
    with contextlib.closing(django.db.connection.cursor()) as cur:
        try:
            rows = fcdjangoutils.jsonview.from_json(request.POST['records'])
            for row in rows:
                check_record(cur, row)

            log_activity(
                request, "check",
                new_rows = [{'API No.': row['API No.'], 'Job Start Dt': row['Job Start Dt'],
                             'State': row['State'], row['County']: row['County']}
                            for row in rows
                            if 'event_guuid' not in row])

            return rows
        except:
            cur.execute('rollback')
            raise
        else:
            cur.execute('commit')

@django.views.decorators.csrf.csrf_exempt
@fcdjangoutils.cors.cors
@track_client
@fcdjangoutils.jsonview.json_view
def parse_pdf(request):
    with contextlib.closing(django.db.connection.cursor()) as cur:
        row = fcdjangoutils.jsonview.from_json(request.POST['row'])
        data = dict(row)

        check_record(cur, data)

        if 'pdf_content' in data: return data

        try:
            logger = fracfocustools.Logger()
            pdf = fracfocustools.FracFocusPDFParser(base64.decodestring(request.POST['pdf']), logger).parse_pdf()

            if not pdf:
                raise Exception("Unable to parse PDF:" + str(logger.messages))

            data.update({'fracture_date': None, 'state': None, 'county': None, 'operator': None, 'well_name': None, 'production_type': None, 'latitude': None, 'longitude': None, 'datum': None, 'true_vertical_depth': None, 'total_water_volume': None})

            data.update(pdf.report_data)
            data['chemicals'] = pdf.chemicals

            data['api'] = data['API No.']

            data['fracture_date'] = parseDate(data['fracture_date'])
            data['latitude'] = parseFloat(data['latitude'])
            data['longitude'] = parseFloat(data['longitude'])
            data['true_vertical_depth'] = parseFloat(data['true_vertical_depth'])
            data['total_water_volume'] = parseFloat(data['total_water_volume'])

            cur.execute("""insert into "FracFocusReport" (pdf_seqid, api, fracture_date, state, county, operator, well_name, production_type, latitude, longitude, datum, true_vertical_depth, total_water_volume) values (%(pdf_seqid)s, %(api)s, %(fracture_date)s, %(state)s, %(county)s, %(operator)s, %(well_name)s, %(production_type)s, %(latitude)s, %(longitude)s, %(datum)s, %(true_vertical_depth)s, %(total_water_volume)s)""", data)

            for rownr, chemical in enumerate(data['chemicals']):
                tmp = {'fracture_date': None, 'row': None, 'trade_name': None, 'supplier': None, 'purpose': None, 'ingredients': None, 'cas_number': None, 'additive_concentration': None, 'hf_fluid_concentration': None, 'comments': None, 'cas_type': None}
                tmp.update(data)
                tmp.update(chemical)
                tmp['row'] = rownr # Yes, the pdf parser seems to stuff this up on some pdfs... reusing row numbers...
                tmp['additive_concentration'] = parseFloat(tmp['additive_concentration'])
                tmp['hf_fluid_concentration'] = parseFloat(tmp['hf_fluid_concentration'])
                cur.execute("""insert into "FracFocusReportChemical" (pdf_seqid, api, fracture_date, row, trade_name, supplier, purpose, ingredients, cas_number, additive_concentration, hf_fluid_concentration, comments, cas_type) values (%(pdf_seqid)s, %(api)s, %(fracture_date)s, %(row)s, %(trade_name)s, %(supplier)s, %(purpose)s, %(ingredients)s, %(cas_number)s, %(additive_concentration)s, %(hf_fluid_concentration)s, %(comments)s, NULL)""", tmp)

            cur.execute("""update "BotTaskStatus" set status='DONE' where task_id=%(pdf_seqid)s and bot='FracFocusReport'""", data)
            cur.execute("""insert into "BotTaskStatus" (task_id, bot, status) values(%(pdf_seqid)s, 'FracFocusPDFDownloader', 'DONE')""", data)
            cur.execute("""insert into "BotTaskStatus" (task_id, bot, status) values(%(pdf_seqid)s, 'FracFocusPDFParser', 'DONE')""", data)

            log_activity(request, "pdf", state=data['state'], county=data['county'], api=data['api'], pdf_seqid=data['pdf_seqid'])

        except:
            cur.execute('rollback')
            raise
        else:
            cur.execute('commit')

        appomatic_siteinfo.management.commands.fracscrapeimport.scrapetoevent(
            appomatic_legacymodels.models.Fracfocusscrape.objects.get(seqid = data['pdf_seqid']),
            appomatic_legacymodels.models.Fracfocusreport.objects.get(pdf_seqid = data['pdf_seqid']),
            appomatic_siteinfo.models.Source.get("FracBot", ""))

        data = row        
        check_record(cur, data)

        return data

@django.views.decorators.csrf.csrf_exempt
@fcdjangoutils.cors.cors
@track_client
@fcdjangoutils.jsonview.json_view
def update_states(request):
    arg = fcdjangoutils.jsonview.from_json(request.POST['arg'])
    added = []
    for id, name in arg['states'].iteritems():
        existing = appomatic_fracbotserver.models.State.objects.filter(name=name)
        if not existing:
            appomatic_fracbotserver.models.State(name=name, siteid=id).save()
            added.append(name)
    if added:
        log_activity(request, "states", names=added)

@django.views.decorators.csrf.csrf_exempt
@fcdjangoutils.cors.cors
@track_client
@fcdjangoutils.jsonview.json_view
def update_counties(request):
    arg = fcdjangoutils.jsonview.from_json(request.POST['arg'])
    state = appomatic_fracbotserver.models.State.objects.get(name=arg['state'])
    added = []
    for id, name in arg['counties'].iteritems():
        existing = appomatic_fracbotserver.models.County.objects.filter(state=state, name=name)
        if not existing:
            appomatic_fracbotserver.models.County(state=state, name=name, siteid=id).save()
            added.append(name)
    if added:
        log_activity(request, "counties", state=arg['state'], names=added)

@django.views.decorators.csrf.csrf_exempt
@fcdjangoutils.cors.cors
@track_client
@fcdjangoutils.jsonview.json_view
def client_log(request):
    log_activity(request, "client-" + request.POST['activity_type'], **fcdjangoutils.jsonview.from_json(request.POST['info']))
