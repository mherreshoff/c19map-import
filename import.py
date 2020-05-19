#!/usr/bin/env python3
import argparse
import collections
import csv
import datetime
import io
import numpy as np
import os
from pathlib import Path
import pickle
import requests
import sys

from util.csv import csv_as_dicts
import util.date as ud
from util.place import Place
from util.recon import PlaceRecon
from util.time_series import TimeSeries

# --------------------------------------------------------------------------------

parser = argparse.ArgumentParser(description='Make time series files from Johns Hopkins University data')
parser.add_argument("--start", default="2020-01-22", type=ud.date_argument)
parser.add_argument("--last", default="today", type=ud.date_argument)
parser.add_argument("--interventions_doc",
        default="1-tj7Cjx3e3eSfFGhhhJTGikaydIclFG1QrD06Eh9oDM")
parser.add_argument("--interventions_sheet", default="Interventions")
parser.add_argument("--population_doc",
        default="1-tj7Cjx3e3eSfFGhhhJTGikaydIclFG1QrD06Eh9oDM")
parser.add_argument("--population_sheet", default="population")
parser.add_argument("--sheets_csv_fetcher", default=(
    "https://docs.google.com/spreadsheets/d/{doc}/gviz/tq?tqx=out:csv&sheet={sheet}"))
parser.add_argument("--JHU_url_format", default=(
    'https://raw.githubusercontent.com/CSSEGISandData/COVID-19/master'+
    '/csse_covid_19_data/csse_covid_19_daily_reports/%m-%d-%Y.csv'))
parser.add_argument('--JHU_data_dir', default='downloads/JHU')
parser.add_argument('--output_csvs', action='store_true')
args = parser.parse_args()


# --------------------------------------------------------------------------------
# Funtions which fetch data from JHU's github and our spreadsheets.

def fetch(source_url, dest_file, cache=False, verbose=True):
    if os.path.exists(dest_file):
        if isinstance(cache, datetime.timedelta):
            last_modified = datetime.datetime.fromtimestamp(os.path.getmtime(dest_file))
            age = datetime.datetime.now() - last_modified
            download = (age >= cache)
        elif isinstance(cache, bool):
            download = not cache
        else:
            raise ValueError(f'Unrecognized cache value: {cache}')
    else:
        Path(dest_file).parent.mkdir(parents=True, exist_ok=True)
        download = True
    if download:
        if verbose:
            print(f'Downloading: {source_url} ---> {dest_file}')
        r = requests.get(source_url)
        r.raise_for_status()
        open(dest_file, 'w').write(r.text)
        return r.text
    else:
        return open(dest_file).read()


def fetch_raw_jhu_data(dates):
    jhu_data = {}
    for date in dates:
        url = date.strftime(args.JHU_url_format)
        file_path = os.path.join(args.JHU_data_dir, date.isoformat() + ".csv")
        try:
            fetch(url, file_path, cache=True)
        except requests.exceptions.HTTPError as e:
            print(f"Couldn't fetch: {url}")
            print(f"The fetch failed with code {e.response.status_code}: {e.response.reason}")
            if e.response.status_code == 404:
                print(f"It seems JHU has not yet published the data for {date.isoformat()}.")
            sys.exit(1)
        jhu_data[date] = csv_as_dicts(open(file_path, encoding='utf-8-sig'))
        # Note: utf-8-sig gets rid of unicode byte order mark characters.
    return jhu_data


def fetch_population_data():
    """Download c19map.org's population data."""
    csv_url = args.sheets_csv_fetcher.format(
        doc=args.population_doc, sheet=args.population_sheet)
    csv_str = fetch(csv_url, 'downloads/population.csv', cache=datetime.timedelta(hours=1))
    csv_source = csv_as_dicts(io.StringIO(csv_str))
    populations = {}
    for row in csv_source:
        country = row["Country/Region"]
        province = row["Province/State"]
        key = (country, province, '')
        populations[key] = int(row["Population"].replace(',', ''))
    return populations


def fetch_intervention_data():
    """Download c19map.org's intervention data."""
    csv_url = args.sheets_csv_fetcher.format(
        doc=args.interventions_doc, sheet=args.interventions_sheet)
    csv_str = fetch(csv_url, 'downloads/interventions.csv', cache=datetime.timedelta(hours=1))
    csv_source = csv_as_dicts(io.StringIO(csv_str))
    date_cols = [(ud.parse_date(s), s) for s in csv_source.headers()]
    date_cols = sorted((d, s) for d, s in date_cols if d is not None)
    for d, d2 in zip(date_cols, date_cols[1:]):
        assert (d2[0]-d[0]).days == 1, "Dates must be consecutive.  Did a column get deleted?"
    start_date = date_cols[0][0]
    unknown = TimeSeries(start_date, ['Unknown']*len(date_cols))

    interventions = {}
    for row in csv_source:
        place = (row['Country/Region'], row['Province/State'], '')
        assert place not in interventions, f"Duplicate row for place {place}"
        intervention_list = []
        prev_state = 'No Intervention'
        for d, s in date_cols:
            cell = row[s]
            if s == '':
                intervention_list.append(prev_state)
            else:
                intervention_list.append(cell)
                prev_state = cell
        interventions[place] = TimeSeries(start_date, intervention_list)
    return interventions, unknown


def fetch_mobility_data():
    csv_str = fetch(
            'https://www.gstatic.com/covid19/mobility/Global_Mobility_Report.csv',
            'downloads/google_mobility.csv', cache=datetime.timedelta(hours=1))
    csv_source = csv_as_dicts(io.StringIO(csv_str))
    # TODO: Segment the rows by region.
    return csv_source


# --------------------------------------------------------------------------------
# Load our inputs:

dates = ud.date_range_inclusive(args.start, args.last)
raw_jhu_data = fetch_raw_jhu_data(dates)
interventions, intervention_unknown = fetch_intervention_data()
intervention_dates = intervention_unknown.dates()
population = fetch_population_data()
mobility_data = fetch_mobility_data()


# --------------------------------------------------------------------------------
# Reconcile the data together into one `Place` object for each region.
recon = PlaceRecon()
places = {}
interventions_recorded = set()
populations_recorded = set()
unknown_interventions_places = set()

new_population = {}
for k, v in population.items():
    k = recon.canonicalize(k)
    if k:
        new_population[k] = v
population = new_population
print()
for k, p in sorted(population.items()):
    print(f'pop{k} ---> {p}')

interventions = {recon.canonicalize(k): v for k,v in interventions.items()}

throw_away_places = set([
    ('US', 'US', ''), ('Australia', '', ''),
    ('Canada', 'Recovered', ''),
    ('US', 'Recovered', ''),
    ])

def first_present(d, ks):
    for k in ks:
        if k in d: return d[k]
    return None

for date, row_source in raw_jhu_data.items():
    for keyed_row in row_source:
        country = first_present(keyed_row, ['Country_Region', 'Country/Region'])
        province = first_present(keyed_row, ['Province_State', 'Province/State'])
        district = first_present(keyed_row, ['Admin2']) or ''
        latitude = first_present(keyed_row, ['Lat', 'Latitude'])
        longitude = first_present(keyed_row, ['Long_', 'Longitude'])
        confirmed = first_present(keyed_row, ['Confirmed'])
        deaths = first_present(keyed_row, ['Deaths'])
        recovered = first_present(keyed_row, ['Recovered'])

        p = (country, province, district)
        if p in throw_away_places: continue
        p = recon.canonicalize(p)
        if p is None: continue

        if p not in places:
            places[p] = Place(dates)
            places[p].set_key(p)
            if p in population:
                places[p].population = population[p]
                populations_recorded.add(p)
            if p in interventions:
                places[p].interventions = interventions[p]
                interventions_recorded.add(p)
            else:
                places[p].interventions = intervention_unknown
                unknown_interventions_places.add(p)

        if latitude is not None and longitude is not None:
            places[p].latitude = latitude
            places[p].longitude = longitude
        places[p].update('confirmed', date, confirmed)
        places[p].update('deaths', date, deaths)
        places[p].update('recovered', date, recovered)

# Sanity check make sure we don't have two synonymous countries left:
import country_converter as coco
countries = sorted(set(p.country for p in places.values()))
iso_codes = coco.convert(names=countries, src='regex', to='iso3')
for i1, c1 in enumerate(countries):
    for i2 in range(i1+1, len(countries)):
        c2 = countries[i2]
        a1 = iso_codes[i1]
        a2 = iso_codes[i2]
        if a1 == a2:
            print(f'Country collision {c1} {c2} --> {a1} {a2}')

for r in sorted(recon.unrecognized_countries):
    print(f"Unrecognized country: {r}")
print()

for r in sorted(recon.place_renames.keys()):
    if r not in recon.used_renames:
        print(f"Unused rename: {r}")
print()

# --------------------------------------------------------------------------------
# Edits to the data to fix various artefacts and glitches.

# Consolidate US county data into states:
def consolidate_to_province_level(country):
    for p in sorted(places.keys()):
        if p[0] == country and p[2] != '':
            state = (p[0], p[1], '')
            if state not in places:
                places[state] = Place(dates)
                places[state].set_key(state)
                if p in population:
                    places[p].population = population[p]
                places[state].interventions = intervention_unknown
            places[state].confirmed += places[p].confirmed
            places[state].deaths += places[p].deaths
            places[state].recovered += places[p].recovered
            del places[p]  # Avoid double-counting.

consolidate_to_province_level("United States")
consolidate_to_province_level("Canada")

def consolidate_to_country_level(country):
    target = places[(country, '', '')]
    for p in sorted(places.keys()):
        if p[0] == country and p[1] != '':
            target.confirmed += places[p].confirmed
            target.deaths += places[p].deaths
            target.recovered += places[p].recovered
            del places[p]

consolidate_to_country_level("Germany")
consolidate_to_country_level("Italy")
consolidate_to_country_level("Spain")


# Fix the fact that France was recorded as French Polynesia on March 23rd:
def correct_misrecorded_place(d, correct_p, recorded_p):
    correct = places[correct_p]
    recorded = places[recorded_p]
    prev_d = d - datetime.timedelta(1)
    correct.confirmed[d] = recorded.confirmed[d]
    correct.deaths[d] = recorded.deaths[d]
    correct.recovered[d] = recorded.recovered[d]
    recorded.confirmed[d] = recorded.confirmed[prev_d]
    recorded.deaths[d] = recorded.deaths[prev_d]
    recorded.recovered[d] = recorded.recovered[prev_d]

correct_misrecorded_place(
        datetime.date(2020, 3, 23),
        ('France', '', ''), ('French Polynesia', '', ''))

# Hubei China suddenly increased their reported deaths on April 17th.
# Until we get better data from before then, we'll scale everything before that date up.
def correct_late_reporting(p, d):
    place = places[p]
    prev_d = d - datetime.timedelta(1)
    scaling_factor = place.deaths[d]/place.deaths[prev_d]
    pos = place.deaths.date_to_position(d)
    new_deaths = place.deaths.array()[:pos] * scaling_factor
    place.deaths.array()[:pos] = new_deaths.astype(int)

correct_late_reporting(('China', 'Hubei', ''), datetime.date(2020, 4, 17))

# --------------------------------------------------------------------------------
# Warnings for catching missing data.

# These are the countries we already weren't bothering to simulate.
# By silencing them, these warnings can flag that a country got misspelt
# in or deleted from the population table (or JHU added a new country.)
SILENCE_POPULATION_WARNINGS = set([
    ('Australia', 'External territories', ''),
    ('Australia', 'Jervis Bay Territory', ''),
    ('France', 'Saint Pierre and Miquelon', ''),
    ('Netherlands', 'Bonaire, Sint Eustatius and Saba', ''),
    ('United Kingdom', 'Anguilla', ''),
    ('United Kingdom', 'British Virgin Islands', ''),
    ('United Kingdom', 'Falkland Islands (Islas Malvinas)', ''),
    ('United Kingdom', 'Falkland Islands (Malvinas)', ''),
    ('United Kingdom', 'Turks and Caicos Islands', ''),
    ('West Bank and Gaza', '', '')])

print()
for k, p in sorted(places.items()):
    if not p.population is None:
        print("No Population Data: ", k)

print()
for k, p in sorted(places.items()):
    if p.interventions is None:
        print("No Intervention Data: ", k)

print()
for p in sorted(interventions.keys()):
    if p not in interventions_recorded:
        print("Lost intervention data for: ", p)

print()
for p in sorted(population.keys()):
    if p not in populations_recorded:
        print("Lost population data for: ", p)


# --------------------------------------------------------------------------------
# Output Data.
print()
for k in sorted(places.keys()):
    print(' --- '.join(k))

with open('places.pkl', 'wb') as pickle_f:
    pickle.dump(places, pickle_f)

if args.output_csvs:
    confirmed_out = csv.writer(open("time_series_confirmed.csv", 'w'))
    deaths_out = csv.writer(open("time_series_deaths.csv", 'w'))
    recovered_out = csv.writer(open("time_series_recovered.csv", 'w'))

    headers = ["Province/State","Country/Region","Lat","Long"]
    headers += [d.strftime("%m/%d/%y") for d in dates]
    confirmed_out.writerow(headers)
    deaths_out.writerow(headers)
    recovered_out.writerow(headers)

    for p in sorted(places.keys()):
        country, province, district = p
        if district: continue
        if sum(places[p].confirmed) == 0: continue  #Skip if no data.

        latitude = places[p].latitude or ''
        longitude = places[p].longitude or ''
        row_start = [province, country, latitude, longitude]
        confirmed_out.writerow(row_start + list(places[p].confirmed))
        deaths_out.writerow(row_start + list(places[p].deaths))
        recovered_out.writerow(row_start + list(places[p].recovered))

