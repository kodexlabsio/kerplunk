"""Generate ontology packs from permissively licensed connector schemas.

Reads four kinds of source, each under a permissive licence, and writes one pack per source system
into ``konstruct/semantics/ontologies/generated/``. It then rebuilds ``ontologies/index.json.gz``
(every pack, hand-written and generated, merged), the pack list in
``docs/guide/ontology-packs.md``, ``ontologies/SOURCES.json`` (each repository read, with its URL,
licence and commit), ``ontologies/NOTICE`` and ``licenses/ontology-pack-sources.txt`` (the texts of
the licences among them other than Apache-2.0):

* Fivetran's dbt packages (https://github.com/fivetran, Apache-2.0): the tables Fivetran lands for
  each source, their columns and descriptions (``models/**/src_*.yml``, with ``{{ doc() }}``
  references resolved from the package's own doc blocks), the tables its staging models read, the
  tables its tests key on more than one column, and the columns of its integration-test seeds.
* Singer and Meltano taps whose repository licence is MIT, Apache-2.0 or BSD: stream names, primary
  keys, property names, types and descriptions, from ``schemas/*.json`` and from the Meltano SDK's
  ``th.PropertiesList(...)`` and ``primary_keys`` in Python.
* dlt's verified sources (https://github.com/dlt-hub/verified-sources, Apache-2.0): resource names,
  primary keys and declared columns.

No source's code is imported or run: YAML and JSON are read as data, Python with ``ast`` and SQL as
text. Each table with a key that names it (``id``, ``uuid``, ``gid`` or ``<table>_id``) is an
entity, unless it is a history, snapshot, report or statistics table, or its source keys it on more
than one column. Each numeric column named as an amount, count or total is a measure. A
``<name>_id`` column that names another of the source's tables is a relationship. Each pack's
``match`` holds blocks of the source's typical tables, each with distinctive columns, and the
script keeps only the blocks that pick out their own pack among all of them.

Usage:
    uv run python .kodex/scripts/generate_packs.py --fivetran fivetran/ --taps taps/ --hub hub/ \\
        --dlt verified-sources/
    uv run python .kodex/scripts/generate_packs.py --index     # after editing a hand-written pack

``fivetran/`` holds clones of the fivetran/dbt_* repositories; ``taps/`` holds clones of tap
repositories, one directory each; ``hub/`` is a clone of meltano/hub, read for each tap's system
name and label.
"""

from __future__ import annotations

import argparse
import ast
import csv
import gzip
import json
import math
import re
import subprocess
import sys
import textwrap
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from konstruct.semantics import registry  # noqa: E402

OUT = registry.PACKS / 'generated'
DOCS = ROOT / 'docs' / 'guide' / 'ontology-packs.md'
SOURCES = registry.PACKS / 'SOURCES.json'
NOTICE = registry.PACKS / 'NOTICE'
LICENCES = ROOT / 'licenses' / 'ontology-pack-sources.txt'
WORD_LIST = ROOT / 'tests' / 'fixtures' / 'pack_words.txt'
PERMISSIVE = ('MIT', 'Apache-2.0', 'BSD-2-Clause', 'BSD-3-Clause')
LOADERS = ('fivetran', 'singer', 'dlt')

# Taps and sources that read a database, files or any API, or are tests and examples, rather than
# one source system with fixed tables.
EXCLUDED = {
    'airbyte',
    'airbyte_wrapper',
    'apachelog',
    'athena',
    'bigquery',
    'cassandra',
    'chess',
    'creation',
    'kaloricketabulky',
    'meteotrentino',
    'trentinopvgis',
    'ex1',
    'marvel',
    'mock_fixture',
    'pagerlistwebscrapper',
    'saneedgedb',
    'pokemon_tcg',
    'rick_and_morty',
    'sharedparentcontext',
    'clickhouse',
    'connector_name',
    'cratedb',
    'csv',
    'csv_folder',
    'dagster',
    'db2',
    'dbf',
    'dbt',
    'dbt_artifacts',
    'diffgen',
    'duckdb',
    'dynamodb',
    'feed',
    'filesystem',
    'firestore',
    'gmail_csv',
    'google_sheets',
    'iceberg',
    'inbox',
    'jaffle_shop',
    'jaffleverse',
    'kafka',
    'kinesis',
    'meltano',
    'meltano_cloud',
    'meltanohub',
    'mongodb',
    'mssql',
    'oracle',
    'parquet',
    'pg_replication',
    'pokemon',
    'postgres',
    'powerbi_metadata',
    'prometheus',
    'rag_hubspot',
    'rag_jira',
    'rag_zendesk',
    'redshift',
    'rest_api',
    'rest_api_msdk',
    'rickandmorty',
    's3_csv',
    'scraping',
    'sharepoint',
    'sharepointsites',
    'smoke_test',
    'snowflake',
    'socrata',
    'spreadsheets',
    'sql_database',
    'stackoverflow_sampledata',
    'universal_file',
    'unstructured_data',
    'webcrawl',
}
# Repositories that describe one organisation's own tables rather than the source system's.
EXCLUDED_REPOSITORIES = {
    'https://github.com/mashey/tap-domo',
    'https://github.com/lumapps/tap-netsuitesuiteql',
    'https://github.com/quentinlumapps/tap-netsuitesuiteql',
}
# What a reader of the manifest should know about a repository's provenance.
NOTES = {
    'https://github.com/dbt-labs/tap-yotpo': (
        'Its stream schemas overlap those of singer-io/tap-yotpo (AGPL-3.0); this repository is '
        'licensed Apache-2.0 by its own LICENSE file, and its stream names, keys and property names '
        'and types are read from it.'
    ),
}
# Product names as their makers spell them, where a source's text does not.
PRODUCT_NAMES = {
    'Bigcommerce': 'BigCommerce',
    'Github': 'GitHub',
    'Gitlab': 'GitLab',
    'Hubspot': 'HubSpot',
    'Linkedin': 'LinkedIn',
    'Netsuite': 'NetSuite',
    'Pagerduty': 'PagerDuty',
    'Paypal': 'PayPal',
    'Quickbooks': 'QuickBooks',
    'Sendgrid': 'SendGrid',
    'Servicenow': 'ServiceNow',
    'Tiktok': 'TikTok',
    'Woocommerce': 'WooCommerce',
    'Youtube': 'YouTube',
}
# Source names that are the same system under another name; the pack takes the value.
ALIASES = {
    'shopify_graphql': 'shopify',
    'ga4_export': 'ga4',
    'facebook': 'facebook_ads',
    'adwords': 'google_ads',
    'googleads': 'google_ads',
    'googleads_python': 'google_ads',
    'linkedin_marketing': 'linkedin_ads',
    'tiktok_business': 'tiktok_ads',
    'pinterest': 'pinterest_ads',
    'redditads': 'reddit_ads',
    'amazon_advertising': 'amazon_ads',
    'instagram': 'instagram_business',
    'bing_ads': 'microsoft_ads',
    'zendesk_singer_sdk': 'zendesk',
    'appstore_singer_sdk': 'apple_store',
    'salesforce_meltano_sdk': 'salesforce',
    'netsuite_msdk': 'netsuite2',
    'netsuitesuiteql': 'netsuite2',
    'asana_dlt': 'asana',
    'shopify_dlt': 'shopify',
    'stripe_analytics': 'stripe',
    'circle_ci': 'circleci',
    'surveymonkey': 'survey_monkey',
    'mws': 'amazon_mws',
    'bamboohr': 'bamboo_hr',
    'amazonads': 'amazon_ads',
    'amazon_advertising_v3': 'amazon_ads',
    'appstore': 'apple_store',
    'bing': 'microsoft_ads',
    'ticketswap_bing_ads': 'microsoft_ads',
    'gmail_incremental': 'gmail',
    'google_analytics_v4': 'google_analytics_4',
}
# Names whose capitals a title cannot infer.
TITLES = {
    'chargebacks': 'Chargebacks911',
    'whatconverts': 'WhatConverts',
    'mavenlink': 'Kantata (formerly Mavenlink)',
    'pardot': 'Account Engagement (formerly Pardot)',
    'activecampaign': 'ActiveCampaign',
    'activeprospect': 'ActiveProspect',
    'adobe_umapi': 'Adobe User Management API',
    'amazon_ads': 'Amazon Ads',
    'amazon_mws': 'Amazon Marketplace Web Service',
    'amazon_selling_partner': 'Amazon Selling Partner',
    'apple_search_ads': 'Apple Search Ads',
    'apple_store': 'App Store Connect',
    'atlassian_scim': 'Atlassian SCIM',
    'avantlink': 'AvantLink',
    'aws_cloud_cost': 'AWS Cloud Cost',
    'aws_cost_explorer': 'AWS Cost Explorer',
    'azuredevops': 'Azure DevOps',
    'bamboo_hr': 'BambooHR',
    'bill': 'Bill.com',
    'betterstack': 'Better Stack',
    'bigcommerce': 'BigCommerce',
    'callrail': 'CallRail',
    'calltrackingmetrics': 'CallTrackingMetrics',
    'captivateiq': 'CaptivateIQ',
    'chatgpt_ads': 'ChatGPT Ads',
    'chorusai': 'Chorus.ai',
    'circleci': 'CircleCI',
    'claricopilot': 'Clari Copilot',
    'claude': 'Claude',
    'clickup': 'ClickUp',
    'cm360': 'Campaign Manager 360',
    'coingecko': 'CoinGecko',
    'crowd_dev': 'crowd.dev',
    'customerio': 'Customer.io',
    'dbt_cloud': 'dbt Cloud',
    'dv360': 'Display & Video 360',
    'dynamics_365_crm': 'Microsoft Dynamics 365 CRM',
    'easyecom': 'EasyEcom',
    'eia': 'EIA',
    'eltoro': 'El Toro',
    'facebook_ads': 'Facebook Ads',
    'facebook_pages': 'Facebook Pages',
    'fec_filing': 'FEC Filing',
    'fivetran_platform': 'Fivetran Platform',
    'fmp': 'Financial Modeling Prep',
    'fred': 'FRED',
    'ga4': 'Google Analytics 4',
    'getresponse': 'GetResponse',
    'github': 'GitHub',
    'gitlab': 'GitLab',
    'gmail': 'Gmail',
    'google_ads': 'Google Ads',
    'google_analytics_4': 'Google Analytics 4 reports',
    'google_play': 'Google Play',
    'greythr': 'greytHR',
    'groundtruth': 'GroundTruth',
    'helpscout': 'Help Scout',
    'hubspot': 'HubSpot',
    'impactradius': 'Impact',
    'instagram_business': 'Instagram Business',
    'jquants': 'J-Quants',
    'leaflink': 'LeafLink',
    'linkedin_ads': 'LinkedIn Ads',
    'linkedin_pages': 'LinkedIn Pages',
    'lucid_scim': 'Lucid SCIM',
    'mailchimp': 'Mailchimp',
    'mailerlite': 'MailerLite',
    'mailjet': 'Mailjet',
    'mercadopago': 'Mercado Pago',
    'messagebird': 'MessageBird',
    'microsoft_ads': 'Microsoft Advertising',
    'mntn': 'MNTN',
    'netsuite': 'NetSuite',
    'netsuite2': 'NetSuite (SuiteAnalytics)',
    'newstore': 'NewStore',
    'noaa_ghcn': 'NOAA GHCN',
    'open_meteo': 'Open-Meteo',
    'openai': 'OpenAI',
    'opensearch': 'OpenSearch',
    'pagerduty': 'PagerDuty',
    'pinterest_ads': 'Pinterest Ads',
    'planetscaleapi': 'PlanetScale',
    'polarsh': 'Polar',
    'powerbi': 'Power BI',
    'purecloud': 'Genesys Cloud',
    'qoben_comeen': 'Comeen',
    'quickbooks': 'QuickBooks',
    'reddit_ads': 'Reddit Ads',
    'salesforce_history': 'Salesforce History',
    'salesforce_marketing_cloud': 'Salesforce Marketing Cloud',
    'sanctionskit': 'SanctionsKit',
    'sap': 'SAP',
    'satismeter': 'SatisMeter',
    'sendgrid': 'SendGrid',
    'service_titan': 'ServiceTitan',
    'servicenow': 'ServiceNow',
    'shipbob': 'ShipBob',
    'shiphero': 'ShipHero',
    'snapchat_ads': 'Snapchat Ads',
    'socketdev': 'Socket',
    'stackadapt': 'StackAdapt',
    'stackerhq': 'Stacker',
    'survey_monkey': 'SurveyMonkey',
    'thetradedesk': 'The Trade Desk',
    'ticktick': 'TickTick',
    'tiktok_ads': 'TikTok Ads',
    'tiktok_shop': 'TikTok Shop',
    'tiktok_social': 'TikTok',
    'travel_perk': 'TravelPerk',
    'twitter': 'X (formerly Twitter)',
    'twitter_ads': 'X Ads (formerly Twitter Ads)',
    'udemy_for_business': 'Udemy Business',
    'vk': 'VK',
    'vk_ads': 'VK Ads',
    'woocommerce': 'WooCommerce',
    'wordpress': 'WordPress',
    'workday_financial_management': 'Workday Financial Management',
    'yahooquery': 'Yahoo Finance (yahooquery)',
    'yfinance': 'Yahoo Finance (yfinance)',
    'youtube_analytics': 'YouTube Analytics',
    'zendesk': 'Zendesk Support',
    'zendesk_sell': 'Zendesk Sell',
    'zoomphone': 'Zoom Phone',
}
# Words a title spells otherwise than in title case.
TITLE_WORDS = {
    'ai': 'AI',
    'api': 'API',
    'aws': 'AWS',
    'cms': 'CMS',
    'crm': 'CRM',
    'dbt': 'dbt',
    'erp': 'ERP',
    'f1': 'F1',
    'hcm': 'HCM',
    'hr': 'HR',
    'io': 'IO',
    'ms': 'Microsoft',
    'nasa': 'NASA',
    'nhl': 'NHL',
    'npm': 'npm',
    'sms': 'SMS',
    'ssb': 'SSB',
    'usgs': 'USGS',
}

# A measure's column name ends in one of these words, perhaps with a unit after it.
MEASURE = re.compile(
    r'(^|_)(amount|amounts|total|subtotal|price|cost|costs|spend|revenue|impressions|clicks|conversions'
    r'|conversion_value|quantity|views|reach|sessions|discount|discounts|tax|taxes|fee|fees|balance'
    r'|budget|count|duration|engagements|likes|shares|installs|downloads|opens|bounces|unsubscribes'
    r'|estimate|time_spent|story_points|points|hours|minutes|seconds|bytes|score)'
    r'(_(usd|cents|micros|in_cents|in_micros|local|seconds|minutes|ms))?$'
)
NOT_MEASURE = re.compile(r'(_id|_ids|_at|_date|_time|_currency|_type|_status|_code|_name|_url|_key)$|^id$')
# A setting, such as a limit, a default, a delay or a rounding, or the time left until something
# happens, rather than something measured.
SETTING = re.compile(
    r'^(min|max|minimum|maximum|default|target|threshold)_|(^|_)(interval_count|ttl|timeout|retention|limit'
    r'|limits|offset|cap|quota|max|min|threshold|rounding|delay|working_hours|settings|auto_renew'
    r'|expires_in)(_|$)'
)
# A description whose first sentence describes a setting: 'Default length of the interview',
# 'Budget settings for the ad group'. A later sentence can name one in passing: 'The number of
# visits to this Page's location. If the Page setting ... is disabled'.
SETTING_TEXT = re.compile(r'^\W*default\b|^[^.]*\bsettings?\b', re.I)
# A description that says a column is a flag, a state or an identifier rather than a quantity, or
# that the column is going away.
NOT_A_QUANTITY = re.compile(
    r'\b(booleans?|true|false|indicates (whether|if)|whether|flag|deprecated|no longer (used|supported)'
    r'|returns? null|will be removed|the id of|identifier)\b',
    re.I,
)
DEPRECATED = re.compile(r'\b(deprecated|no longer (used|supported|available)|will be removed|sunset)\b', re.I)
# Tables that hold versions, snapshots or aggregates of an object rather than the object itself.
NOT_AN_ENTITY = re.compile(
    r'(^|_)(history|histories|snapshot|snapshots|report|reports|stats|stat|statistics|insights|insight'
    r'|daily|hourly|weekly|monthly|yearly|performance|metrics|metric|analytics|by|breakdown|breakdowns'
    r'|trend|trends|summary|summaries|aggregate|aggregates|changelog|versions|version|revisions)(_|$)'
)
HISTORY = re.compile(r'\b(history|historical|each version|every version|snapshots?)\b', re.I)
HISTORY_NAME = re.compile(r'(^|_)(history|histories|snapshot|snapshots|versions|version|revisions)(_|$)')
# A description of a table with a row per period, or per one thing per another.
NOT_ONE_ROW = re.compile(
    r'\b(daily|hourly|weekly|monthly)\b|\bper \w+[,;]?(?: \w+[,;]?){0,3} per\b|\bnot a distinct\b', re.I
)
# A staging model's description that names its grain: 'The grain of this model is id, index,
# source_relation.'
GRAIN = re.compile(r'\bgrain of this (?:model|table) is (\w+(?:, \w+)+)', re.I)
# A value in a seed row that a numeric column could hold.
NUMBER = re.compile(r'[-+]?(\d{1,3}(,\d{3})+(\.\d*)?|\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?')
# Columns that give a table a grain of time, so its key repeats from one period to the next.
DATE_GRAIN = ({'day'}, {'date'}, {'date_day'}, {'start_time', 'end_time'})
NUMERIC = ('number', 'integer')
# A column more sources than this have is too common to tell one from another.
SPREAD = 6
BRITISH = {
    'organization': 'organisation',
    'organizations': 'organisations',
    'organizational': 'organisational',
    'organize': 'organise',
    'organized': 'organised',
    'organizes': 'organises',
    'organizing': 'organising',
    'behavior': 'behaviour',
    'behaviors': 'behaviours',
    'canceled': 'cancelled',
    'canceling': 'cancelling',
    'color': 'colour',
    'colors': 'colours',
    'analyze': 'analyse',
    'analyzed': 'analysed',
    'analyzing': 'analysing',
    'categorize': 'categorise',
    'categorized': 'categorised',
    'license': 'licence',
    'licenses': 'licences',
    'authorize': 'authorise',
    'authorized': 'authorised',
    'authorization': 'authorisation',
    'authorizations': 'authorisations',
    'customize': 'customise',
    'customized': 'customised',
    'customization': 'customisation',
    'prioritize': 'prioritise',
    'prioritized': 'prioritised',
    'recognize': 'recognise',
    'recognized': 'recognised',
    'summarize': 'summarise',
    'summarized': 'summarised',
    'synchronize': 'synchronise',
    'synchronized': 'synchronised',
    'synchronization': 'synchronisation',
    'utilize': 'utilise',
    'utilized': 'utilised',
    'utilization': 'utilisation',
    'normalized': 'normalised',
    'standardized': 'standardised',
    'optimized': 'optimised',
    'optimization': 'optimisation',
    'initialized': 'initialised',
    'finalized': 'finalised',
    'personalized': 'personalised',
    'center': 'centre',
    'centers': 'centres',
    'favorite': 'favourite',
    'favorites': 'favourites',
    'labeled': 'labelled',
    'labeling': 'labelling',
    'modeling': 'modelling',
    'modeled': 'modelled',
    'fulfillment': 'fulfilment',
    'fulfillments': 'fulfilments',
    'enrollment': 'enrolment',
    'enrollments': 'enrolments',
    'gray': 'grey',
    'aging': 'ageing',
    'catalog': 'catalogue',
    'catalogs': 'catalogues',
    'traveled': 'travelled',
    'itemized': 'itemised',
    'uncategorized': 'uncategorised',
    'categorization': 'categorisation',
    'capitalized': 'capitalised',
    'centralized': 'centralised',
    'localized': 'localised',
    'localization': 'localisation',
    'maximize': 'maximise',
    'minimize': 'minimise',
    'monetized': 'monetised',
    'monetization': 'monetisation',
    'serialized': 'serialised',
    'specialized': 'specialised',
    'tokenized': 'tokenised',
    'visualization': 'visualisation',
    'visualizations': 'visualisations',
    'anonymized': 'anonymised',
    'emphasize': 'emphasise',
    'defense': 'defence',
    'jewelry': 'jewellery',
    'cancelation': 'cancellation',
}
# A sentence that may list a field's values keeps their spelling, as does anything quoted.
LISTS_VALUES = re.compile(r'\b(values?|one of|possible|options?|enum|e\.g\.|i\.e\.|such as|either)\b|=', re.I)
QUOTED = re.compile(r"""(`[^`]*`|"[^"]*"|(?<!\w)'[^']*'(?!\w))""")
# Words whose first letter sounds otherwise: 'an hour', 'a user', 'a one-off'.
AN_WORDS = {'hour', 'hours', 'hourly', 'honest', 'honour', 'heir', 'herb'}
A_PREFIXES = ('uni', 'use', 'usa', 'usu', 'uti', 'uri', 'url', 'utm', 'uuid', 'eur', 'one', 'once', 'ubi')
AN_PREFIXES = ('unin', 'unim')
# Lower-case abbreviations said letter by letter: 'an SMS', 'an SKU'.
LETTERS = {
    'sms',
    'sku',
    'seo',
    'sla',
    'sql',
    'mql',
    'nps',
    'rsvp',
    'xml',
    'html',
    'http',
    'hr',
    'hris',
    'mrr',
}
# Words before the last that make a name a phrase, whose last word keeps its plural.
PHRASE = {'of', 'on', 'to', 'per', 'by', 'for', 'in', 'from', 'with', 'vs'}
# Abbreviations in table and column names, in full.
ABBREVIATIONS = {
    'acct': 'account',
    'grmember': 'group member',
    'whos': 'who is',
    'inh': 'inherited',
    'pmax': 'Performance Max',
    'repo': 'repository',
    'repos': 'repositories',
    'skus': 'SKUs',
    'trx': 'transaction',
    'unsub': 'unsubscribe',
    'subscribedskus': 'subscribed SKUs',
    'amt': 'amount',
    'avg': 'average',
    'bcy': 'base currency',
    'cnt': 'count',
    'num': 'number',
    'pct': 'percent',
    'pres': 'presentment',
    'prem': 'premises',
    'qty': 'quantity',
    'stmt': 'statement',
    'sys': 'system',
    'txn': 'transaction',
    'txns': 'transactions',
}
# Words of two letters a run of words may hold: campaignid, accountsummaryby.
SHORT_WORDS = {'id', 'ip', 'of', 'on', 'to', 'in', 'by', 'at', 'ad', 'up', 'no', 'or', 'is', 'us', 'ui', 'tv'}
# Acronyms, written in capitals in a name.
ACRONYMS = set(
    (
        'ach api apm arn bxgy brb cik cmdb dcf dma etf fba fbk icd iam gid mms npm tcs tms wbs arr asn aws bic cac cdn cpa cpc cpe cpl cpm cpu cpv crm csat csv ctr cvr dau dns eps '
        'erp esg eta eur faq fx gbp gcm gdpr gl gmv gpu gst hr hris hst html http https iban id ids ip ipo '
        'irr json kpi kyc lms ltv mau mfa mql mrr nps ocr ooo otp pdf pe pii pnl pos ppc pst qa rfm roas roi '
        'rsvp saml scim sdk seo sip sku sla sms sql sso ssn tcp ttm ui uk uri url urls us usd utc utm uuid '
        'vat vpn vwap wau xml ytd'
    ).split()
)
# Words of a name that a product spells its own way.
WORDS = {
    'alibaba': 'Alibaba',
    'benzinga': 'Benzinga',
    'capex': 'capex',
    'digg': 'Digg',
    'emailer': 'emailer',
    'emoji': 'emoji',
    'heroku': 'Heroku',
    'mysql': 'MySQL',
    'oauth': 'OAuth',
    'php': 'PHP',
    'piotroski': 'Piotroski',
    'rehydrated': 'rehydrated',
    'unsubscriber': 'unsubscriber',
    'vsphere': 'vSphere',
    'android': 'Android',
    'facebook': 'Facebook',
    'github': 'GitHub',
    'gitlab': 'GitLab',
    'google': 'Google',
    'hubspot': 'HubSpot',
    'instagram': 'Instagram',
    'ios': 'iOS',
    'iphone': 'iPhone',
    'linkedin': 'LinkedIn',
    'netsuite': 'NetSuite',
    'paypal': 'PayPal',
    'salesforce': 'Salesforce',
    'shopify': 'Shopify',
    'slack': 'Slack',
    'stripe': 'Stripe',
    'tiktok': 'TikTok',
    'twitter': 'Twitter',
    'whatsapp': 'WhatsApp',
    'youtube': 'YouTube',
    'zendesk': 'Zendesk',
    'appsec': 'AppSec',
    'azure': 'Azure',
    'cmdb': 'CMDB',
    'cta': 'CTA',
    'eeoc': 'EEOC',
    'fargate': 'Fargate',
    'gcp': 'GCP',
    'ipos': 'IPOs',
}
IRREGULAR = {
    'ads': 'ad',
    'addresses': 'address',
    'aliases': 'alias',
    'analyses': 'analysis',
    'axes': 'axis',
    'bonuses': 'bonus',
    'buses': 'bus',
    'businesses': 'business',
    'campuses': 'campus',
    'canvases': 'canvas',
    'chaos': 'chaos',
    'children': 'child',
    'criteria': 'criterion',
    'data': 'data',
    'indices': 'index',
    'kudos': 'kudos',
    'leaves': 'leave',
    'media': 'media',
    'news': 'news',
    'people': 'person',
    'quizzes': 'quiz',
    'series': 'series',
    'species': 'species',
    'statuses': 'status',
    'viruses': 'virus',
}


# Licences and repositories

LICENCE_FILE = re.compile(r'(?i)^(licen[cs]e|copying)(\.(md|txt|rst))?$')


def licence_of(directory: Path) -> str:
    """A repository's licence, read from its LICENSE file."""
    files = [f for f in directory.iterdir() if f.is_file() and LICENCE_FILE.match(f.name)]
    if not files:
        return 'none'
    text = ' '.join(' '.join(f.read_text(encoding='utf-8', errors='replace').split()) for f in files).lower()
    if 'gnu' in text or 'elastic license' in text or 'mozilla public' in text:
        return 'other'
    if 'apache license' in text and 'version 2.0' in text:
        return 'Apache-2.0'
    if 'permission is hereby granted, free of charge' in text:
        return 'MIT'
    if 'redistribution and use in source and binary forms' in text:
        return 'BSD-3-Clause' if 'endorse or promote' in text else 'BSD-2-Clause'
    return 'other'


def licence_text(directory: Path) -> str:
    for f in sorted(directory.iterdir()):
        if f.is_file() and LICENCE_FILE.match(f.name):
            return f.read_text(encoding='utf-8', errors='replace').strip()
    return ''


def _git(directory: Path, *args: str) -> str:
    result = subprocess.run(['git', '-C', str(directory), *args], capture_output=True, text=True, check=False)
    return result.stdout.strip()


def repository(directory: Path) -> tuple[str, str]:
    """A checkout's URL, without ``.git``, and its commit."""
    return _git(directory, 'remote', 'get-url', 'origin').removesuffix('.git'), _git(
        directory, 'rev-parse', 'HEAD'
    )


@dataclass
class Stream:
    """One table as one source describes it."""

    name: str
    """The table's name as the loader lands it, in lower case."""
    key: list[str] = field(default_factory=list)
    columns: dict[str, str] = field(default_factory=dict)
    """Each column (lower case) and its type: number, integer, string, boolean, object, array or
    unknown."""
    descriptions: dict[str, str] = field(default_factory=dict)
    description: str | None = None
    source_name: str = ''
    """The table's name in a dbt source, which can differ from the name it lands as."""
    typical: int = 0
    """How central a typical sync's table it is: for Fivetran, 1 for a table its staging models read
    by default, and more for each other model that uses it."""
    composite: bool = False
    """Whether the source's own tests key it on more than one column."""
    repeats: set[str] = field(default_factory=set)
    """The identifier columns whose values repeat in the source's sample rows: not keys."""
    sample: set[str] = field(default_factory=set)
    """The columns of the source's sample of the table, such as a dbt package's test seed: the
    columns a real sync lands."""


@dataclass
class Origin:
    """One repository's description of one source system."""

    loader: str
    """fivetran, singer or dlt."""
    system: str
    repository: str
    licence: str
    commit: str
    path: str = ''
    """The part of the repository read, for a repository of many sources."""
    streams: dict[str, Stream] = field(default_factory=dict)
    schema: str | None = None
    title: str | None = None
    text: str = ''
    """The repository's licence text, kept for any licence but Apache-2.0, whose text carries no
    copyright notice."""


def system_name(name: str) -> str:
    name = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    return ALIASES.get(name, name)


# Fivetran

DOC_BLOCK = re.compile(r'{%-?\s*docs\s+(\w+)\s*-?%}(.*?){%-?\s*enddocs\s*-?%}', re.S)
DOC_REF = re.compile(r"""{{\s*doc\(\s*['"](\w+)['"]\s*\)\s*}}""")
DEFAULT_VAR = re.compile(r"""{{\s*var\(\s*['"][\w.]+['"]\s*,\s*['"]([\w.]+)['"]\s*\)\s*}}""")


def described(value: Any, blocks: dict[str, str]) -> str | None:
    """A description with its ``{{ doc() }}`` references resolved, or None where one cannot be."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    whole = DOC_REF.fullmatch(value)
    if whole:
        value = blocks.get(whole.group(1), '')
    elif DOC_REF.search(value):
        # Only a reference that stands as a word of its own: one pasted inside a word is a slip.
        for m in DOC_REF.finditer(value):
            if (m.start() and not value[m.start() - 1].isspace()) or (
                m.end() < len(value) and value[m.end()].isalnum()
            ):
                return None
        value = DOC_REF.sub(lambda m: blocks.get(m.group(1), '\0'), value)
    if '\0' in value or '{{' in value or '{%' in value:
        return None
    value = ' '.join(value.split())
    return value or None


SOURCE_CALL = r"""source\(\s*['"]\w+['"]\s*,\s*['"](\w+)['"]\s*\)"""


def _sql_tables(text: str, variables: dict[str, str]) -> set[str]:
    """The source tables a staging model reads: by Fivetran's macros, by ``source()``, or by a
    variable that the package's dbt_project.yml sets to a ``source()``."""
    found = set(re.findall(r"""(?:table_identifier|single_table_name)\s*=\s*['"](\w+)['"]""", text))
    found |= set(re.findall(SOURCE_CALL, text))
    found |= {variables[v] for v in re.findall(r"""var\(\s*['"](\w+)['"]\s*\)""", text) if v in variables}
    return {t.lower() for t in found}


def staging(repo: Path) -> tuple[Counter[str], set[str]]:
    """The source tables a package's staging models read when its variables keep their defaults,
    each with how many of the package's other models use it, and the tables its tests key on more
    than one column."""
    reads: dict[str, set[str]] = {}
    refs: dict[str, set[str]] = {}
    off: set[str] = set()
    project = repo / 'dbt_project.yml'
    variables = dict(
        re.findall(r"""^\s*(\w+)\s*:\s*["']?{{\s*""" + SOURCE_CALL, project.read_text(encoding='utf-8'), re.M)
        if project.is_file()
        else []
    )
    for path in sorted(repo.glob('models/**/stg_*.sql')):
        text = path.read_text(encoding='utf-8', errors='replace')
        reads[path.stem] = _sql_tables(text, variables)
        refs[path.stem] = set(re.findall(r"""ref\(\s*['"](\w+)['"]\s*\)""", text))
        if re.search(r"""enabled\s*=\s*var\(\s*['"][\w.]+['"]\s*,\s*False\s*\)""", text):
            off.add(path.stem)

    def tables(model: str, seen: frozenset[str] = frozenset()) -> set[str]:
        if model in seen or model in off:
            return set()
        out = set(reads.get(model, ()))
        for ref in refs.get(model, ()):
            out |= tables(ref, seen | {model})
        return out

    used: Counter[str] = Counter()
    for path in sorted(repo.glob('models/**/*.sql')):
        if not path.stem.startswith('stg_'):
            used.update(
                set(
                    re.findall(
                        r"""ref\(\s*['"](\w+)['"]\s*\)""", path.read_text(encoding='utf-8', errors='replace')
                    )
                )
            )
    typical: Counter[str] = Counter()
    for model in reads:
        if not model.endswith('_tmp'):
            for table in tables(model):
                typical[table] = max(typical[table], used[model] + 1)
    composite: set[str] = set()
    for path in sorted(repo.glob('models/**/*.yml')):
        try:
            data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        except yaml.YAMLError:
            continue
        for model in (data.get('models') or []) if isinstance(data, dict) else []:
            if not isinstance(model, dict) or not str(model.get('name', '')).startswith('stg_'):
                continue
            # A grain of more than one column, as the model's description gives it.
            grain = GRAIN.search(' '.join(str(model.get('description') or '').split()))
            if grain and len({c for c in grain.group(1).split(', ') if c != 'source_relation'}) > 1:
                read = tables(str(model['name']))
                if len(read) == 1:
                    composite |= read
            for test in (model.get('tests') or []) + (model.get('data_tests') or []):
                spec = test.get('dbt_utils.unique_combination_of_columns') if isinstance(test, dict) else None
                if not isinstance(spec, dict):
                    continue
                columns = spec.get('combination_of_columns') or (spec.get('arguments') or {}).get(
                    'combination_of_columns'
                )
                if isinstance(columns, list) and len([c for c in columns if c != 'source_relation']) > 1:
                    read = tables(str(model['name']))
                    if len(read) == 1:
                        composite |= read
    return typical, composite


def seeds(repo: Path) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    """The columns of each integration-test seed of a dbt package, by the seed's name without
    ``_data`` or a warehouse's name, the identifier columns whose values repeat in its rows, and the
    columns whose values are mostly not numbers."""
    columns: dict[str, set[str]] = {}
    repeats: dict[str, set[str]] = {}
    strings: dict[str, set[str]] = {}
    for path in sorted((repo / 'integration_tests' / 'seeds').glob('**/*.csv')):
        name = re.sub(r'_(postgres|snowflake|bigquery|redshift|databricks|spark)$', '', path.stem.lower())
        name = re.sub(r'_data$', '', name)
        with path.open(encoding='utf-8', errors='replace', newline='') as handle:
            rows = list(csv.reader(handle))
        if not rows:
            continue
        header = [c.strip().strip('"').lower() for c in rows[0]]
        columns.setdefault(name, set()).update(c for c in header if c)
        for i, column in enumerate(header):
            if column == 'id' or column.endswith('_id') or column.endswith('id'):
                values = [r[i] for r in rows[1:] if i < len(r) and r[i].strip()]
                if len(values) != len(set(values)):
                    repeats.setdefault(name, set()).add(column)
            # A seed can hold a few malformed numbers to test the package's casts (Amazon's 39.01a
            # among a hundred prices): a column is text when most of its values are.
            values = [r[i].strip() for r in rows[1:] if i < len(r) and r[i].strip()]
            if column and 2 * sum(not NUMBER.fullmatch(v) for v in values) > len(values):
                strings.setdefault(name, set()).add(column)
    return columns, repeats, strings


def _sample(stream: Stream, samples: dict[str, set[str]], source: str) -> set[str]:
    # A seed is named for its table, perhaps after the source's name or its first words:
    # youtube_video for youtube_analytics's video.
    words = source.split('_')
    prefixes = ['', *(f'{"_".join(words[:n])}_' for n in range(len(words), 0, -1))]
    for name in (stream.source_name, stream.name):
        for seed in (f'{prefix}{name}' for prefix in prefixes):
            if seed in samples:
                return samples[seed]
    return set()


def read_fivetran(directory: Path) -> list[Origin]:
    """Each Fivetran source's tables, from its dbt packages under Apache-2.0."""
    origins: dict[str, Origin] = {}
    # Facts any package of a source holds: an old dbt_<name>_source package can hold the tests.
    typicals: dict[str, Counter[str]] = {}
    composites: dict[str, set[str]] = {}
    samples: dict[str, dict[str, set[str]]] = {}
    repeating: dict[str, dict[str, set[str]]] = {}
    textual: dict[str, dict[str, set[str]]] = {}
    for repo in sorted(p for p in directory.iterdir() if (p / 'models').is_dir()):
        if licence_of(repo) != 'Apache-2.0':
            continue
        blocks: dict[str, str] = {}
        for md in sorted(repo.glob('models/**/*.md')):
            for name, body in DOC_BLOCK.findall(md.read_text(encoding='utf-8', errors='replace')):
                blocks.setdefault(name, ' '.join(body.split()))
        typical, composite = staging(repo)
        seeded, repeated, strings = seeds(repo)
        url, commit = repository(repo)
        readme = repo / 'README.md'
        heading = (
            re.search(r'^# (.+?) dbt Package', readme.read_text(encoding='utf-8'), re.M)
            if readme.is_file()
            else None
        )
        for path in sorted(repo.glob('models/**/src_*.yml')):
            if 'integration_tests' in path.parts or 'dbt_packages' in path.parts:
                continue
            try:
                data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
            except yaml.YAMLError:
                continue
            for source in data.get('sources') or []:
                if not isinstance(source, dict) or not source.get('name'):
                    continue
                if str(source.get('loader') or 'fivetran').lower() != 'fivetran':
                    continue
                name = system_name(str(source['name']))
                if name in EXCLUDED:
                    continue
                schema = DEFAULT_VAR.search(str(source.get('schema') or ''))
                origin = Origin(
                    'fivetran',
                    name,
                    url,
                    'Apache-2.0',
                    commit,
                    schema=schema.group(1) if schema else str(source['name']).lower(),
                    title=heading.group(1).strip() if heading else None,
                )
                for table in source.get('tables') or []:
                    if not isinstance(table, dict) or not table.get('name'):
                        continue
                    dbt_name = str(table['name']).lower()
                    identifier = DEFAULT_VAR.search(str(table.get('identifier') or ''))
                    landed = identifier.group(1) if identifier else str(table.get('identifier') or dbt_name)
                    landed = landed.lower()
                    if not re.fullmatch(r'[a-z0-9_]+', landed):
                        continue
                    columns = [c for c in table.get('columns') or [] if isinstance(c, dict) and c.get('name')]
                    descriptions = {
                        str(c['name']).lower(): described(c.get('description'), blocks) for c in columns
                    }
                    origin.streams[landed] = Stream(
                        landed,
                        columns={str(c['name']).lower(): 'unknown' for c in columns},
                        descriptions={k: v for k, v in descriptions.items() if v},
                        description=described(table.get('description'), blocks),
                        source_name=dbt_name,
                    )
                for seed, columns_ in seeded.items():
                    samples.setdefault(name, {}).setdefault(seed, set()).update(columns_)
                for seed, columns_ in repeated.items():
                    repeating.setdefault(name, {}).setdefault(seed, set()).update(columns_)
                for seed, columns_ in strings.items():
                    textual.setdefault(name, {}).setdefault(seed, set()).update(columns_)
                held_typical = typicals.setdefault(name, Counter())
                for table_, weight in typical.items():
                    held_typical[table_] = max(held_typical[table_], weight)
                composites.setdefault(name, set()).update(composite)
                if not origin.streams:
                    continue
                held = origins.get(name)
                # A source in both an old dbt_<name>_source package and the newer dbt_<name>
                # package: the one that describes more columns wins.
                size = sum(len(s.columns) for s in origin.streams.values())
                if held is None or size > sum(len(s.columns) for s in held.streams.values()):
                    origins[name] = origin
    for name, origin in origins.items():
        for stream in origin.streams.values():
            stream.typical = typicals.get(name, Counter())[stream.source_name]
            stream.composite = stream.source_name in composites.get(name, ())
            stream.sample = _sample(stream, samples.get(name, {}), name)
            stream.repeats = _sample(stream, repeating.get(name, {}), name)
            # Fivetran's sources give no types: a column a seed holds text in is not a number.
            for column in _sample(stream, textual.get(name, {}), name) & set(stream.columns):
                stream.columns[column] = 'string'
    return list(origins.values())


# JSON schemas, for the Singer taps


def _kind(prop: Any) -> str:
    if not isinstance(prop, dict):
        return 'unknown'
    kind = prop.get('type')
    if isinstance(kind, list):
        kind = next((k for k in kind if k != 'null'), 'null')
    if kind is None and ('anyOf' in prop or 'oneOf' in prop):
        options = [_kind(o) for o in prop.get('anyOf') or prop.get('oneOf') or []]
        options = [o for o in options if o != 'null']
        return options[0] if options else 'unknown'
    return str(kind or 'unknown')


def schema_columns(schema: Any) -> tuple[dict[str, str], dict[str, str]]:
    """A JSON schema's top-level properties, with their types and descriptions."""
    props = schema.get('properties') if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return {}, {}
    columns = {str(k).lower(): _kind(v) for k, v in props.items()}
    descriptions = {
        str(k).lower(): ' '.join(v['description'].split())
        for k, v in props.items()
        if isinstance(v, dict) and isinstance(v.get('description'), str) and v['description'].strip()
    }
    return columns, descriptions


def table_name(name: str) -> str | None:
    """A stream's name as a destination names its table."""
    name = re.sub(r'[\s.\-]+', '_', name.strip()).lower()
    return name if re.fullmatch(r'[a-z][a-z0-9_]*', name) else None


def _keys(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.lower()]
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            if isinstance(item, str):
                out.append(item.lower())
            elif isinstance(item, list) and item and all(isinstance(x, str) for x in item):
                out.append('.'.join(item).lower())
        return out
    return []


# Singer and Meltano taps, and dlt's sources, read with ast and never run

SDK_TYPES = {
    'IntegerType': 'integer',
    'NumberType': 'number',
    'DecimalType': 'number',
    'BooleanType': 'boolean',
    'ObjectType': 'object',
    'ArrayType': 'array',
}
NAME_FIELDS = (
    'name',
    'tap_stream_id',
    'stream_name',
    'stream_id',
    'table_name',
    'TABLE',
    'STREAM_NAME',
    'TABLE_NAME',
)
KEY_FIELDS = (
    'primary_keys',
    'key_properties',
    'KEY_PROPERTIES',
    'PRIMARY_KEYS',
    'primary_key',
    'pk_fields',
    'key_property',
    'KEY_PROPERTY',
    'PRIMARY_KEY',
)
SCHEMA_FILE_FIELDS = (
    'schema_filepath',
    'schema_path',
    'SCHEMA_PATH',
    'schema_file',
    'SCHEMA_FILE',
    'schema_name',
)
SKIPPED_DIRS = {'tests', 'test', 'docs', 'examples', '.venv', 'venv', 'build', 'integration_tests', '.git'}


def _literal(node: ast.AST | None) -> Any:
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ''


def _assigned(body: list[ast.stmt]) -> dict[str, ast.expr]:
    names: dict[str, ast.expr] = {}
    for node in body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            names[node.target.id] = node.value
    return names


class Source:
    """The Python files of one repository, parsed and never run."""

    def __init__(self, directory: Path) -> None:
        self.modules: list[tuple[ast.Module, dict[str, ast.expr]]] = []
        for path in sorted(directory.rglob('*.py')):
            parts = {p.lower() for p in path.relative_to(directory).parts}
            if parts & SKIPPED_DIRS or path.name in ('setup.py', 'noxfile.py', 'conftest.py'):
                continue
            if path.name.startswith('test_'):
                continue
            try:
                tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
            except (SyntaxError, ValueError, RecursionError):
                continue
            self.modules.append((tree, _assigned(tree.body)))
        self.classes: dict[str, tuple[ast.ClassDef, dict[str, ast.expr]]] = {}
        for tree, names in self.modules:
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    self.classes.setdefault(node.name, (node, names))

    def lookup(self, name: str, module: dict[str, ast.expr]) -> ast.expr | None:
        if name in module:
            return module[name]
        return next((names[name] for _, names in self.modules if name in names), None)

    def attrs(
        self, name: str, seen: frozenset[str] = frozenset()
    ) -> dict[str, tuple[ast.expr, dict[str, ast.expr]]]:
        """A class's attributes as its body assigns them, over its bases' in the same repository."""
        if name in seen or name not in self.classes:
            return {}
        node, module = self.classes[name]
        out: dict[str, tuple[ast.expr, dict[str, ast.expr]]] = {}
        for base in node.bases:
            out.update(self.attrs(_call_name(base), seen | {name}))
        out.update({k: (v, module) for k, v in _assigned(node.body).items()})
        return out

    def properties(
        self, node: ast.AST, module: dict[str, ast.expr], depth: int = 0
    ) -> dict[str, tuple[str, str | None]]:
        """The top-level properties of a Meltano SDK ``PropertiesList`` expression."""
        if depth > 8:
            return {}
        if isinstance(node, (ast.Name, ast.Attribute)):
            # schemas.charges, or a name imported from the module that assigns it
            target = self.lookup(node.id if isinstance(node, ast.Name) else node.attr, module)
            return self.properties(target, module, depth + 1) if target is not None else {}
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'to_dict'
        ):
            return self.properties(node.func.value, module, depth + 1)
        items: list[ast.expr]
        if isinstance(node, (ast.List, ast.Tuple)):
            items = list(node.elts)
        elif isinstance(node, ast.Call) and _call_name(node.func) == 'PropertiesList':
            items = list(node.args)
        else:
            return {}
        out: dict[str, tuple[str, str | None]] = {}
        for item in items:
            if isinstance(item, ast.Starred):
                out.update(self.properties(item.value, module, depth + 1))
            elif isinstance(item, ast.Name):
                target = self.lookup(item.id, module)
                if isinstance(target, ast.Call):
                    out.update(_property(target))
            elif isinstance(item, ast.Call):
                out.update(_property(item))
        return out


def _property(call: ast.Call) -> dict[str, tuple[str, str | None]]:
    if not _call_name(call.func).endswith('Property') or not call.args:
        return {}
    name = _literal(call.args[0])
    if not isinstance(name, str):
        return {}
    wrapped = next((k.value for k in call.keywords if k.arg == 'wrapped'), None)
    of = call.args[1] if len(call.args) > 1 else wrapped
    kind = SDK_TYPES.get(_call_name(of), 'string') if of is not None else 'unknown'
    description = next((_literal(k.value) for k in call.keywords if k.arg == 'description'), None)
    return {name.lower(): (kind, description if isinstance(description, str) else None)}


def _json_schemas(directory: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in sorted(directory.rglob('*.json')):
        parts = [p.lower() for p in path.relative_to(directory).parts]
        if 'schemas' not in parts or set(parts) & (SKIPPED_DIRS | {'shared', 'definitions'}):
            continue
        found.setdefault(re.sub(r'\.schema$', '', path.stem).lower(), path)
    return found


def _read_json(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    try:
        return schema_columns(json.loads(path.read_text(encoding='utf-8')))
    except (ValueError, RecursionError):
        return {}, {}


def _catalogue_entries(directory: Path) -> list[tuple[str, Any, Any]]:
    """The streams of the Singer catalogues kept in a repository, such as a sample catalogue or a
    test's snapshot of one: each stream's name, key properties and schema."""
    out: list[tuple[str, Any, Any]] = []
    for path in sorted(directory.rglob('*.json')):
        if {'node_modules', '.git'} & set(path.parts) or path.stat().st_size > 5_000_000:
            continue
        if 'catalog' not in path.name.lower() and 'catalog' not in str(path.parent).lower():
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, RecursionError):
            continue
        entries = data.get('streams') if isinstance(data, dict) and 'streams' in data else [data]
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not isinstance(entry.get('schema'), dict):
                continue
            bracketed = re.search(r'\[([\w-]+)\]', path.stem)
            name = (
                entry.get('stream')
                or entry.get('tap_stream_id')
                or (bracketed.group(1) if bracketed else None)
            )
            key = entry.get('key_properties')
            for item in entry.get('metadata') or []:
                if (
                    isinstance(item, dict)
                    and item.get('breadcrumb') == []
                    and isinstance(item.get('metadata'), dict)
                ):
                    key = key or item['metadata'].get('table-key-properties')
            if isinstance(name, str):
                out.append((name, key, entry['schema']))
    return out


def read_tap(directory: Path) -> dict[str, Stream]:
    """A tap's streams: from its classes (Meltano SDK and singer-python), its dicts of stream
    settings and its JSON schemas."""
    source = Source(directory)
    schemas = _json_schemas(directory)
    found: dict[str, Stream] = {}

    def add(
        name: Any, key: Any, columns: dict[str, tuple[str, str | None]] | None, schema_file: str | None
    ) -> None:
        table = table_name(name) if isinstance(name, str) and '{' not in name else None
        if table is None:
            return
        stream = found.setdefault(table, Stream(table))
        if key is not None and not stream.key:
            stream.key = _keys(key)
        if columns and not stream.columns:
            stream.columns = {k: v[0] for k, v in columns.items()}
            stream.descriptions = {
                k: ' '.join(v[1].split()) for k, v in columns.items() if v[1] and v[1].strip()
            }
        path = (schemas.get(schema_file.lower()) if schema_file else None) or schemas.get(name.lower())
        if path is not None and not stream.columns:
            stream.columns, stream.descriptions = _read_json(path)

    for class_name in source.classes:
        attrs = source.attrs(class_name)
        name = next((_literal(attrs[f][0]) for f in NAME_FIELDS if f in attrs), None)
        if not isinstance(name, str):
            continue
        key = next((_literal(attrs[f][0]) for f in KEY_FIELDS if f in attrs), None)
        columns = source.properties(*attrs['schema']) if 'schema' in attrs else None
        schema_file = None
        for f in SCHEMA_FILE_FIELDS:
            if f in attrs:
                strings = [
                    n.value
                    for n in ast.walk(attrs[f][0])
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                ]
                schema_file = next(
                    (re.sub(r'(\.schema)?(\.json)?$', '', Path(s).name) for s in strings), None
                )
        if key is None and not columns and schema_file is None and name.lower() not in schemas:
            continue
        add(name, key, columns, schema_file)
    for tree, module in source.modules:
        # Stream settings kept in dicts: {'charges': {'key_properties': ['id']}}, or
        # {'name': 'charges', 'primary_key': 'id'}.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            items = {_literal(k): v for k, v in zip(node.keys, node.values, strict=False) if k is not None}
            keyed = next((items[f] for f in KEY_FIELDS if f in items), None)
            if keyed is not None and isinstance(_literal(items.get('name')), str):
                add(_literal(items['name']), _literal(keyed), None, None)
            for stream_name, value in items.items():
                if not isinstance(stream_name, str) or not isinstance(value, ast.Dict):
                    continue
                inner = {
                    _literal(k): v for k, v in zip(value.keys, value.values, strict=False) if k is not None
                }
                inner_key = next((inner[f] for f in KEY_FIELDS if f in inner), None)
                if inner_key is not None:
                    add(stream_name, _literal(inner_key), None, None)
        # KEY_PROPERTIES = {'charges': ['id'], ...}
        for target, value in module.items():
            if re.search(r'KEY|PK|PRIMARY', target) and isinstance(value, ast.Dict):
                literal = _literal(value)
                if isinstance(literal, dict):
                    for stream_name, key in literal.items():
                        if isinstance(stream_name, str) and isinstance(key, (list, tuple, str)):
                            add(stream_name, key, None, None)
    for name, key, schema in _catalogue_entries(directory):
        table = table_name(name) if isinstance(name, str) else None
        if table is None:
            continue
        stream = found.setdefault(table, Stream(table))
        stream.key = stream.key or _keys(key)
        if not stream.columns:
            stream.columns, stream.descriptions = schema_columns(schema)
    if not found:
        # A classic tap with a schema file for each stream and its keys in its catalogue.
        for stem, path in schemas.items():
            typed, texts = _read_json(path)
            table = table_name(stem)
            if table and typed:
                found[table] = Stream(table, columns=typed, descriptions=texts)
    return {k: v for k, v in found.items() if v.columns or v.key}


def tap_system(name: str) -> str:
    """The source system a tap reads, from its name: tap-google-ads, pipelinewise-tap-zendesk."""
    name = re.split(r'(?:^|[-_])tap[-_]', name.lower())[-1]
    before = None
    while before != name:
        before = name
        name = re.sub(
            r'[-_](singer[-_]sdk|msdk|sdk|meltano|singer|json|backup|forked|legacy|incremental|ab[-_]variant|v\d+)$',
            '',
            name,
        )
    return system_name(name)


def read_taps(taps: Path, hub: Path | None) -> list[Origin]:
    """The Singer and Meltano taps under a permissive licence."""
    labels: dict[str, tuple[str, str | None]] = {}
    if hub is not None:
        for path in sorted((hub / '_data' / 'meltano' / 'extractors').glob('*/*.yml')):
            data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
            url = str(data.get('repo') or '').strip().rstrip('/').removesuffix('.git').lower()
            if url:
                labels.setdefault(url, (path.parent.name, data.get('label')))
    origins = []
    for directory in sorted(p for p in taps.iterdir() if p.is_dir()):
        licence = licence_of(directory)
        if licence not in PERMISSIVE:
            continue
        url, commit = repository(directory)
        if url.lower() in EXCLUDED_REPOSITORIES:
            continue
        hub_name, label = labels.get(url.lower(), (url.rsplit('/', 1)[-1], None))
        system = tap_system(hub_name)
        if system in EXCLUDED:
            continue
        streams = read_tap(directory)
        if streams:
            text = licence_text(directory) if licence != 'Apache-2.0' else ''
            origins.append(
                Origin('singer', system, url, licence, commit, streams=streams, title=label, text=text)
            )
    return origins


DLT_TYPES = {
    'bigint': 'integer',
    'double': 'number',
    'decimal': 'number',
    'bool': 'boolean',
    'text': 'string',
}


def read_dlt(directory: Path) -> list[Origin]:
    """dlt's verified sources: each resource's name, primary key and declared columns."""
    if licence_of(directory) != 'Apache-2.0':
        return []
    url, commit = repository(directory)
    origins = []
    for folder in sorted(p for p in (directory / 'sources').iterdir() if p.is_dir()):
        system = system_name(folder.name)
        if system in EXCLUDED:
            continue
        origin = Origin('dlt', system, url, 'Apache-2.0', commit, path=str(folder.relative_to(directory)))
        for tree, _ in Source(folder).modules:
            for node in ast.walk(tree):
                calls: list[tuple[ast.Call, str | None]] = []
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    calls += [
                        (d, node.name)
                        for d in node.decorator_list
                        if isinstance(d, ast.Call) and _call_name(d.func) == 'resource'
                    ]
                elif isinstance(node, ast.Call) and _call_name(node.func) == 'resource' and node.args:
                    calls.append((node, None))
                elif isinstance(node, ast.Dict):
                    items = {
                        _literal(k): v for k, v in zip(node.keys, node.values, strict=False) if k is not None
                    }
                    if 'name' in items and 'primary_key' in items:
                        name = _literal(items['name'])
                        table = table_name(name) if isinstance(name, str) else None
                        if table:
                            origin.streams.setdefault(
                                table, Stream(table, key=_keys(_literal(items['primary_key'])))
                            )
                for call, default in calls:
                    kw = {k.arg: k.value for k in call.keywords if k.arg}
                    name = _literal(kw['name']) if 'name' in kw else default
                    table = table_name(name) if isinstance(name, str) else None
                    if table is None:
                        continue
                    stream = origin.streams.setdefault(table, Stream(table))
                    stream.key = stream.key or _keys(_literal(kw.get('primary_key')))
                    columns = _literal(kw.get('columns'))
                    if isinstance(columns, dict):
                        stream.columns = {
                            str(c).lower(): DLT_TYPES.get(
                                str(v.get('data_type')), str(v.get('data_type') or 'unknown')
                            )
                            for c, v in columns.items()
                            if isinstance(v, dict)
                        }
        if origin.streams:
            origins.append(origin)
    return origins


# Text


def singular(word: str) -> str:
    """The singular of a plural English noun, as table names use them: warehouses, statuses,
    categories, matches, and employmentstatuses, whose last word is irregular."""
    lower = word.lower()
    if lower in IRREGULAR:
        return IRREGULAR[lower]
    for plural, one in IRREGULAR.items():
        if len(plural) > 4 and lower.endswith(plural) and plural not in ('series', 'species', 'news'):
            return word[: len(word) - len(plural)] + one
    if len(lower) <= 3 or not lower.endswith('s') or lower.endswith(('ss', 'us', 'is')):
        return word
    if lower.endswith('ies') and len(lower) > 4:
        return word[:-3] + 'y'
    if lower.endswith(('sses', 'xes', 'ches', 'shes', 'zzes')):
        return word[:-2]
    return word[:-1]


def singular_name(name: str) -> str:
    """A table's name with its last word singular: ad_groups is ad_group. A phrase keeps its
    plural: price_to_earnings, return_on_assets, though a name that starts with one of its words is
    not a phrase: on_prem_agents."""
    head, _, last = name.rpartition('_')
    if PHRASE & set(head.split('_')[1:]):
        return name
    return f'{head}_{singular(last)}' if head else singular(last)


VOCABULARY: Counter[str] = Counter()
"""The words of the sources' descriptions, with how often each is used: the likelier of two ways
to tell apart the words of a name run together."""
DICTIONARY: set[str] = set()
"""English words, from the word list ``--words`` names."""
UNKNOWN: Counter[str] = Counter()
SPELLINGS = {w.lower() for w in WORDS.values()}
"""The tokens of names that are neither words nor acronyms, nor words run together."""


def known(word: str) -> bool:
    """Whether a token is a word, an acronym or a product's name."""
    return (
        word in DICTIONARY or word in ACRONYMS or word in WORDS or word in SPELLINGS or VOCABULARY[word] >= 5
    )


def segment(token: str) -> list[str]:
    """A token of a name as the words it runs together: devicestatusinfo is device, status and
    info. A word, an acronym, an abbreviation or a number stays as it is."""
    if token in ABBREVIATIONS:
        return ABBREVIATIONS[token].split()
    if not DICTIONARY or not token.isalpha() or len(token) <= 2:
        return [token]
    if any(known(form) for form in (token, singular(token))):
        return [token]
    total = sum(VOCABULARY.values()) + 1
    best: list[tuple[float, list[str]] | None] = [(0.0, [])] + [None] * len(token)
    for end in range(1, len(token) + 1):
        for start in range(max(0, end - 24), end):
            word, held = token[start:end], best[start]
            if held is None or not (known(word) or known(singular(word))):
                continue
            if len(word) < 3 and word not in SHORT_WORDS:
                continue
            # The likelier the words in the sources' own descriptions, and the fewer, the better.
            cost = held[0] - math.log((VOCABULARY[word] + 1) / total) + 4
            if best[end] is None or cost < best[end][0]:  # type: ignore[index]
                best[end] = (cost, [*held[1], *ABBREVIATIONS.get(word, word).split()])
    found = best[len(token)]
    if found is None:
        UNKNOWN[token] += 1
        return [token]
    return found[1]


def spell(word: str) -> str:
    """A word of a name as it is written: an acronym in capitals, a product as its maker
    spells it, an American spelling in British."""
    if word in ACRONYMS:
        return word.upper()
    return WORDS.get(word) or BRITISH.get(word, word)


def human(name: str) -> str:
    """A name in words: each token of a table or column name, the words a token runs together,
    acronyms in capitals and British spelling. Plural words inside the name stay plural (sales
    order, futures exchange), and a letter before a number is one word (channel basic A2)."""
    tokens: list[str] = []
    for token in (t for t in re.split(r'[_\s]+', name) if t):
        if tokens and token.isdigit() and re.fullmatch(r'[a-z]', tokens[-1]):
            tokens[-1] += token
            continue
        tokens.append(token)
    words = [w for t in tokens for w in (segment(t) if not re.fullmatch(r'[a-z]\d+', t) else [t])]
    return ' '.join(w.upper() if re.fullmatch(r'[a-z]\d+', w) else spell(w) for w in words)


def article(phrase: str) -> str:
    """'a' or 'an' for a phrase, by the sound of its first word: 'an hour', 'a user', 'an SMS'."""
    word = re.sub(r'[^A-Za-z0-9]', '', phrase.split()[0]) if phrase.split() else ''
    if not word:
        return 'a'
    if word.isupper() and len(word) > 1 and (len(word) <= 3 or not re.search('[AEIOU]', word)):
        # An abbreviation said letter by letter: an SMS, a URL.
        return 'an' if word[0] in 'AEFHILMNORSX' else 'a'
    lower = word.lower()
    if lower in LETTERS:
        return 'an' if lower[0] in 'aefhilmnorsx' else 'a'
    if lower in AN_WORDS or lower.startswith(AN_PREFIXES):
        return 'an'
    if lower.startswith(A_PREFIXES):
        return 'a'
    return 'an' if lower[0] in 'aeiou' else 'a'


def fix_articles(text: str) -> str:
    """Each 'a' and 'an' in a text, made to fit the word after it."""

    def repl(m: re.Match[str]) -> str:
        wanted = article(m.group(2))
        return f'{wanted.capitalize() if m.group(1)[0].isupper() else wanted} {m.group(2)}'

    return re.sub(r'\b([Aa]n?) (?=[A-Za-z])([A-Za-z][\w-]*)', repl, text)


EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)
# Characters beyond ASCII a description keeps: currency signs and the like, and accented letters.
KEPT = set('\u00a3\u00a5\u00b0\u00d7\u20ac' + ''.join(chr(c) for c in range(0xC0, 0x250) if chr(c).isalpha()))
QUOTES = {chr(0x2019): "'", chr(0x2018): "'", chr(0x201C): '"', chr(0x201D): '"'}


def british(sentence: str) -> str:
    """One sentence in British spelling, unless it may list a field's values, which keep the
    source's spelling."""
    if LISTS_VALUES.search(sentence):
        return sentence
    parts = QUOTED.split(sentence)
    for i in range(0, len(parts), 2):
        for us, uk in BRITISH.items():
            parts[i] = re.sub(rf'(?<![\w`]){re.escape(us)}(?![\w`])', uk, parts[i])
    sentence = ''.join(parts)
    # A capitalised word only at the start, and not before another capitalised word, which would
    # make it a name: Help Center.
    first = re.match(r'([A-Z][a-z]+)\b(?!\s+[A-Z])', sentence)
    if first and first.group(1).lower() in BRITISH:
        sentence = BRITISH[first.group(1).lower()].capitalize() + sentence[first.end() :]
    return sentence


def prose(text: str) -> str:
    """A source's description in this repository's style: British spelling, no dashes, whole
    sentences of at most about 400 characters."""
    text = re.sub(rf'\s*{EM_DASH}\s*', ', ', text)
    text = re.sub(rf'(\d)\s*{EN_DASH}\s*(\d)', r'\1 to \2', text)
    text = text.replace(EN_DASH, ', ')
    text = re.sub(r'\s+--+\s+', ', ', text)
    for curly, plain in QUOTES.items():
        text = text.replace(curly, plain)
    text = ' '.join(
        ''.join(
            ch
            for ch in text
            if ord(ch) < 0x80 or ch in KEPT or (unicodedata.category(ch) == 'Ll' and ord(ch) < 0x250)
        ).split()
    )
    for wrong, right in PRODUCT_NAMES.items():
        text = re.sub(rf'\b{wrong}\b', right, text)
    kept: list[str] = []
    for sentence in re.split(r'(?<=[.!?])\s+(?=[A-Z])', text):
        if kept and len(' '.join(kept)) + len(sentence) > 400:
            break
        kept.append(sentence)
    text = fix_articles(' '.join(british(s) for s in kept))
    if text[:1].islower():
        text = text[0].upper() + text[1:]
    return text if text.endswith(('.', '?', '!')) else text + '.'


def title_of(name: str, origins: list[Origin]) -> str:
    if name in TITLES:
        return TITLES[name]
    for loader in ('singer', 'fivetran'):
        for origin in origins:
            title = (origin.title or '').strip()
            if origin.loader == loader and title and not re.search(r'\b(Api|Crm|Hr|Io|Dbt)\b', title):
                return title
    return ' '.join(TITLE_WORDS.get(w, w.capitalize()) for w in name.split('_'))


# Packs


@dataclass
class Table:
    """One of a source's tables, as each origin names and lands it."""

    concept: str
    streams: dict[int, Stream] = field(default_factory=dict)
    """Each origin, by its place in the pack's list, and the table in it."""
    loaders: dict[int, str] = field(default_factory=dict)

    def order(self) -> list[int]:
        return sorted(self.streams, key=lambda i: (LOADERS.index(self.loaders[i]), i))

    @property
    def spec(self) -> str:
        return '|'.join(dict.fromkeys(self.streams[i].name for i in self.order()))

    @property
    def columns(self) -> set[str]:
        return set().union(*(set(s.columns) for s in self.streams.values()))

    def description(self) -> str | None:
        return next((self.streams[i].description for i in self.order() if self.streams[i].description), None)

    def column_description(self, column: str) -> str | None:
        return next(
            (
                self.streams[i].descriptions[column]
                for i in self.order()
                if column in self.streams[i].descriptions
            ),
            None,
        )

    def types(self, column: str) -> set[str]:
        return {
            s.columns[column] for s in self.streams.values() if s.columns.get(column, 'unknown') != 'unknown'
        }

    def keys(self) -> dict[str, list[str]]:
        """Each accepted key column, with the names of the table keyed on it. Fivetran keys a table
        on id, or on a column named after it (NetSuite's account_id); another loader's declared key
        counts only when it is id, uuid or gid or is named after the table."""
        squashed = self.concept.replace('_', '')
        named = {f'{self.concept}_id', f'{squashed}id', f'{squashed}_id'}
        out: dict[str, list[str]] = {}
        for i in self.order():
            stream = self.streams[i]
            if self.loaders[i] == 'fivetran':
                key = next((k for k in ('id', *sorted(named)) if k in stream.columns), None)
            elif stream.key:
                key = (
                    stream.key[0]
                    if len(stream.key) == 1 and stream.key[0] in {'id', 'uuid', 'gid', *named}
                    else None
                )
                if key is not None and stream.columns and key not in stream.columns:
                    key = None
            else:
                # A tap that declares its keys in a catalogue rather than its code: its id column.
                key = next((k for k in ('id', *sorted(named)) if k in stream.columns), None)
            if key is not None and stream.name not in out.setdefault(key, []):
                out[key].append(stream.name)
        return out

    def is_history(self) -> bool:
        """Whether the table keeps a row for each version or snapshot of an object."""
        for stream in self.streams.values():
            if HISTORY_NAME.search(stream.name) or {'_fivetran_active', '_fivetran_start'} <= set(
                stream.columns
            ):
                return True
            if stream.description and HISTORY.search(stream.description):
                return True
        return bool(HISTORY_NAME.search(self.concept))

    def is_entity(self) -> bool:
        """Whether the table holds one row for each instance of something: not a history,
        snapshot, report or statistics table, nor one with a grain of time (a day, or a start and
        end time), nor one its source keys on more than one column or whose key repeats in the
        source's sample rows, nor one the source deprecates."""
        if NOT_AN_ENTITY.search(self.concept) or any(
            NOT_AN_ENTITY.search(s.name) for s in self.streams.values()
        ):
            return False
        keys = set(self.keys())
        for stream in self.streams.values():
            if keys & stream.repeats:
                return False
            # A bare id beside a day repeats from one day to the next; a key named after the table
            # (Xero's invoice_id beside its date) does not.
            if keys <= {'id', 'uuid', 'gid'} and any(grain <= set(stream.columns) for grain in DATE_GRAIN):
                return False
            if stream.description and NOT_ONE_ROW.search(stream.description):
                return False
            # Fivetran's history mode adds _fivetran_active, _fivetran_start and _fivetran_end and
            # keeps a row for each version. A package that documents only _fivetran_active reads the
            # table either way, and by default it holds one row for each object.
            if stream.composite or {'_fivetran_active', '_fivetran_start'} <= set(stream.columns):
                return False
            if stream.description and (
                DEPRECATED.search(stream.description) or HISTORY.search(stream.description)
            ):
                return False
        return bool(self.keys())


def tables_of(origins: list[Origin]) -> dict[str, Table]:
    tables: dict[str, Table] = {}
    concepts = {singular_name(name) for origin in origins for name in origin.streams}
    heads = {c.rpartition('_')[2] for c in concepts}

    def concept_of(name: str) -> str:
        # A first word in the plural that names another of the source's tables is one of them:
        # users_role is a user role and channels_messages a channel message.
        concept = singular_name(name)
        first, _, rest = concept.partition('_')
        one = singular(first)
        if rest and one != first and one in heads and f'{one}_{rest}' not in concepts:
            return f'{one}_{rest}'
        return concept

    for i, origin in enumerate(origins):
        for name, stream in origin.streams.items():
            if re.match(r'(zz|old|deprecated|legacy|tmp|temp|test)_', name):
                continue  # a table its source keeps out of the way
            concept = concept_of(name)
            item = tables.setdefault(concept, Table(concept))
            item.streams.setdefault(i, stream)
            item.loaders.setdefault(i, origin.loader)
    return tables


def _who(origins: list[Origin]) -> str:
    """The loaders that land a pack's tables, with the verb: 'Fivetran and Singer taps land'."""
    taps = sum(o.loader == 'singer' for o in origins)
    words = {
        'fivetran': 'Fivetran',
        'singer': 'Singer taps' if taps > 1 else 'a Singer tap',
        'dlt': 'dlt',
    }
    names = [words[loader] for loader in dict.fromkeys(o.loader for o in origins)]
    if len(names) == 1:
        return names[0] + (' land' if names[0] == 'Singer taps' else ' lands')
    return ', '.join(names[:-1]) + ' and ' + names[-1] + ' land'


def _count(n: int, word: str, plural: str) -> str:
    return f'{n:,} {word if n == 1 else plural}'


def term_name(title: str, words: str) -> str:
    """A term's name: the source's title, then the words of its table and column without those
    that repeat the title or each other (Reddit Ads and ads_reports give 'Reddit Ads reports')."""
    words = re.sub(r'\b(\w+)( \1\b)+', r'\1', words)
    head, tail = title.lower().split(), words.split()
    for size in range(min(len(head), len(tail)), 0, -1):
        if head[-size:] == tail[:size]:
            if tail[size:]:
                return f'{title} {" ".join(tail[size:])}'
            # The whole table is the title's last word: TikTok Shop's shop.
            return f"{title}'s {words}"
    return f'{title} {words}'


def upper_first(text: str) -> str:
    """Text with its first letter in capitals and the rest as it is: 'GCP host count', though a
    product spelt with a small first letter keeps it: 'vSphere host count'."""
    return text if re.match(r'[a-z]+[A-Z]', text) else text[:1].upper() + text[1:]


def measure_name(concept: str, column: str) -> str:
    """A measure's name from its table and column, without the words they share: order_item and
    item_price_amount give 'order item price amount'; a column that only repeats its table's name
    is the table's value."""
    table, words = concept.split('_'), column.split('_')
    for size in range(min(len(table), len(words)), 0, -1):
        if [singular(w) for w in table[-size:]] == [singular(w) for w in words[:size]]:
            words = words[size:]
            break
    named = f'{human(concept)} {human("_".join(words)) if words else "value"}'
    # A column flattened from a field of the same name: bid_amount_amount.
    return re.sub(r'\b(\w+)( \1\b)+', r'\1', named)


def entity_definition(table: Table, title: str, key: str) -> str:
    text = table.description()
    if text and not DEPRECATED.search(text):
        return prose(text)
    words = human(table.concept)
    keyed = f'its {human(key)}'
    return f'{article(words).capitalize()} {words} in {title}, one row per {words}, keyed on {keyed}.'


def pack_for(name: str, title: str, origins: list[Origin], tables: dict[str, Table]) -> dict[str, Any] | None:
    terms: list[dict[str, Any]] = []
    used: set[str] = set()
    targets: dict[str, str] = {}  # concept -> the <table spec>.<key> a relationship points to
    for concept, table in sorted(tables.items()):
        if not table.is_entity():
            continue
        name_ = term_name(title, human(concept))
        if name_.lower() in used:
            continue
        used.add(name_.lower())
        accepted = table.keys()
        key, names = next(iter(accepted.items()))
        targets[concept] = f'{"|".join(names)}.{key}'
        terms.append(
            {
                'name': name_,
                'kind': 'entity',
                'definition': entity_definition(table, title, key),
                'columns': [f'{"|".join(n)}.{k}' for k, n in accepted.items()],
            }
        )
    for concept, table in sorted(tables.items()):
        # A history or snapshot table repeats each object's figures once for each version.
        if table.is_history() or any(
            s.description and DEPRECATED.search(s.description) for s in table.streams.values()
        ):
            continue
        for column in sorted(table.columns):
            if column.startswith('_') or not MEASURE.search(column) or NOT_MEASURE.search(column):
                continue
            if SETTING.search(column):
                continue
            types = table.types(column)
            text = table.column_description(column)
            # A quantity is numeric where a source types it, and is described or typed at all.
            if (types and not types <= set(NUMERIC)) or (not text and not types):
                continue
            if text and (NOT_A_QUANTITY.search(text) or SETTING_TEXT.search(text)):
                continue
            # A report or history table is named as it is, not as one thing: 'ad group stats clicks'.
            base = concept if table.is_entity() else next(iter(table.streams.values())).name
            name_ = term_name(title, measure_name(base, column))
            if name_.lower() in used:
                continue
            used.add(name_.lower())
            holders = list(dict.fromkeys(s.name for s in table.streams.values() if column in s.columns))
            terms.append(
                {
                    'name': name_,
                    'kind': 'measure',
                    'definition': prose(text)
                    if text
                    else f'{upper_first(human(column))}, as the `{holders[0]}` table of {title} records it.',
                    'columns': [f'{"|".join(holders)}.{column}'],
                }
            )
    relationships = []
    squashed = {c.replace('_', ''): c for c in targets}
    for concept, table in sorted(tables.items()):
        own = set(table.keys())
        for column in sorted(table.columns):
            if column in own or column.startswith('_'):
                continue
            target = None
            if column.endswith('_id'):
                target = singular_name(column[:-3])
            elif column.endswith('id') and len(column) > 4:
                target = squashed.get(singular(column[:-2]))
            if target in targets and target != concept:
                spec = '|'.join(dict.fromkeys(s.name for s in table.streams.values() if column in s.columns))
                relationships.append({'from': f'{spec}.{column}', 'to': targets[target]})
    entities = sum(1 for t in terms if t['kind'] == 'entity')
    if len(terms) < 2:
        return None
    held = [
        _count(n, one, many)
        for n, one, many in (
            (entities, 'entity', 'entities'),
            (len(terms) - entities, 'measure', 'measures'),
            (len(relationships), 'relationship', 'relationships'),
        )
        if n
    ]
    return {
        'name': name,
        'title': title,
        'description': f'The {title} tables that {_who(origins)}, with '
        + (held[0] if len(held) == 1 else f'{", ".join(held[:-1])} and {held[-1]}')
        + '.',
        'schemas': sorted({f'*{name}*', *(f'*{o.schema}*' for o in origins if o.schema)}),
        'origins': list(dict.fromkeys(o.loader for o in origins)),
        'match': [],
        'terms': terms,
        'relationships': relationships,
    }


def choose_match(
    pack: dict[str, Any],
    origins: list[Origin],
    tables: dict[str, Table],
    table_counts: Counter[str],
    column_counts: Counter[str],
) -> None:
    """Match blocks for each origin, from the tables a typical sync lands: those the source's test
    seeds hold, then those Fivetran's staging models read (the more of its models use one, the
    better), then entities before reports, then those other tables name most. Each table is
    required with its key, if it has one, and its rarest columns that are not generic (of those its
    seed holds, when it has one), two of them or three without a key. The blocks are the best
    three tables alone and the best two together, for a source no one table tells apart."""
    named: Counter[str] = Counter(rel['to'].rsplit('.', 1)[0] for rel in pack['relationships'])
    typical = {c: max(s.typical for s in t.streams.values()) for c, t in tables.items()}
    blocks: list[dict[str, list[str]]] = []
    for i in range(len(origins)):
        candidates = []
        for concept, table in tables.items():
            if i not in table.streams:
                continue
            stream = table.streams[i]
            entity = table.is_entity()
            key = (
                next((k for k, names in table.keys().items() if stream.name in names), None)
                if entity
                else None
            )
            # A seed's columns that the source defines: a seed also holds one account's custom fields.
            sampled = stream.sample & set(stream.columns)
            distinctive = sorted(
                (
                    c
                    for c in (sampled if len(sampled) > 3 else stream.columns)
                    if c != key
                    and not c.startswith(('_', 'custom_'))
                    and c not in registry.GENERIC_COLUMNS
                    and column_counts[c] <= SPREAD
                    and re.fullmatch(r'[a-z0-9_]+', c)
                ),
                key=lambda c: (column_counts[c], c),
            )
            if len(distinctive) < (2 if key else 3):
                continue
            spec = next((s for s in named if stream.name in s.split('|')), stream.name)
            rank = (
                not stream.sample,
                not typical[concept],
                -typical[concept],
                not entity,
                registry.is_generic_table(stream.name),
                -named[spec],
                table_counts[stream.name],
                -len(stream.columns),
                stream.name,
            )
            candidates.append((rank, stream.name, [key, *distinctive[:2]] if key else distinctive[:3]))
        candidates.sort()
        for chosen in [[c] for c in candidates[:3]] + [candidates[:2]]:
            block = {name: columns for _, name, columns in chosen}
            if block and block not in blocks:
                blocks.append(block)
    pack['match'] = blocks


def distinct(packs: list[dict[str, Any]]) -> None:
    """Drop each match block that does not pick out its own pack among all of them, and each pack
    left with none."""
    changed = True
    while changed:
        changed = False
        loaded = [registry.read_pack(p, p['name'], 'generated') for p in packs]
        for pack, data in zip(loaded, packs, strict=True):
            kept = []
            for block in pack.match:
                tables = {spec.split('|')[0]: set(columns) for spec, columns in block.items()}
                # A block of generic table names alone holds only in a schema named for the source.
                schema = f'db.{pack.name}' if registry.needs_name(block) else 'db.raw'
                found = registry.find(loaded, {schema: tables}, {schema: set(tables)})
                if [m.pack.name for m in found] == [pack.name]:
                    kept.append(block)
                else:
                    print(
                        f'not distinct: {pack.name} {sorted(tables)}: {[m.pack.name for m in found]}',
                        file=sys.stderr,
                    )
            if len(kept) != len(pack.match):
                data['match'] = kept
                changed = True
        packs[:] = [p for p in packs if p['match']]


def generate(
    fivetran: Path, taps: Path | None, hub: Path | None, dlt: Path | None, words: Path | None = None
) -> None:
    origins = read_fivetran(fivetran)
    origins += read_taps(taps, hub) if taps else []
    origins += read_dlt(dlt) if dlt else []
    by_system: dict[str, list[Origin]] = {}
    for origin in origins:
        by_system.setdefault(origin.system, []).append(origin)
        for stream in origin.streams.values():
            for text in (stream.description or '', *stream.descriptions.values()):
                VOCABULARY.update(w.lower() for w in re.findall(r'[A-Z]?[a-z]{2,}|[A-Z]{2,}(?![a-z])', text))
    table_counts: Counter[str] = Counter()
    column_counts: Counter[str] = Counter()
    every: dict[str, dict[str, Table]] = {}
    for name, held in by_system.items():
        held.sort(key=lambda o: (LOADERS.index(o.loader), o.repository.lower(), o.path))
        every[name] = tables_of(held)
        # How many sources have each table and column name.
        table_counts.update({s.name for o in held for s in o.streams.values()})
        column_counts.update({c for o in held for s in o.streams.values() for c in s.columns})
    packs = []
    for name, held in sorted(by_system.items()):
        pack = pack_for(name, title_of(name, held), held, every[name])
        if pack is not None:
            choose_match(pack, held, every[name], table_counts, column_counts)
            if pack['match']:
                packs.append(pack)
    distinct(packs)
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob('*.yml'):
        old.unlink()
    header = (
        '# Generated by .kodex/scripts/generate_packs.py from the repositories ../SOURCES.json lists;\n'
        '# see ../NOTICE. Do not edit: write terms in ../{name}.yml instead.\n'
    )
    for pack in packs:
        (OUT / f'{pack["name"]}.yml').write_text(
            header.format(name=pack['name'])
            + yaml.dump(pack, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=110),
            encoding='utf-8',
        )
    write_sources({p['name']: by_system[p['name']] for p in packs}, hub, words)
    write_words(registry.build_index())
    print(f'{len(packs)} packs written', file=sys.stderr)
    if UNKNOWN:
        print('not words:', ' '.join(f'{w}:{n}' for w, n in UNKNOWN.most_common(80)), file=sys.stderr)


def write_words(packs: list[dict[str, Any]]) -> None:
    """The words the packs' term names use that are English words, for the test that every word
    of a name is a word or an acronym."""
    found: set[str] = set()
    for pack in packs:
        for term in pack['terms']:
            for word in re.findall(r'[A-Za-z]+', term['name'].removeprefix(pack['title'])):
                lower = word.lower()
                if (
                    known(lower)
                    or known(singular(lower))
                    or lower in BRITISH.values()
                    or lower.removesuffix('s') in ACRONYMS
                ):
                    found.add(lower)
    WORD_LIST.write_text(''.join(f'{w}\n' for w in sorted(found)), encoding='utf-8')


class Dumper(yaml.SafeDumper):
    """YAML without anchors and aliases (``&id001``), which a list shared by two terms would get."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def write_sources(used: dict[str, list[Origin]], hub: Path | None, words: Path | None) -> None:
    """The manifest of every repository the packs come from, the notice that credits them, and the
    texts of their licences other than Apache-2.0."""
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    if words is not None:
        url, commit = repository(words.parent)
        entries[(url, '')] = {
            'repository': url,
            'path': words.name,
            'licence': 'Unlicense',
            'commit': commit,
            'loader': 'word list',
            'used for': 'telling apart the words of a name run together, such as transactionline',
        }
    if hub is not None:
        url, commit = repository(hub)
        entries[(url, '')] = {
            'repository': url,
            'licence': licence_of(hub),
            'commit': commit,
            'loader': 'meltano hub',
            'used for': "each tap's source system and label",
        }
    texts: dict[str, tuple[str, str]] = {}
    for name, origins in sorted(used.items()):
        for origin in origins:
            entry = entries.setdefault(
                (origin.repository, origin.path),
                {
                    'repository': origin.repository,
                    **({'path': origin.path} if origin.path else {}),
                    'licence': origin.licence,
                    'commit': origin.commit,
                    'loader': origin.loader,
                    'packs': [],
                },
            )
            if name not in entry['packs']:
                entry['packs'].append(name)
            if origin.repository in NOTES:
                entry['note'] = NOTES[origin.repository]
            if origin.text:
                texts[origin.repository] = (origin.licence, origin.text)
    data = sorted(entries.values(), key=lambda e: (e['loader'], e['repository'].lower(), e.get('path', '')))
    SOURCES.write_text(json.dumps(data, indent=1) + '\n', encoding='utf-8')
    rule = '=' * 100
    LICENCES.write_text(
        'The licences of the repositories the generated ontology packs are made from, other than those\n'
        'under the Apache License 2.0 (Apache-2.0.txt), as konstruct/semantics/ontologies/SOURCES.json\n'
        'lists them.\n\n'
        + '\n\n'.join(
            f'{rule}\n{url} ({licence})\n{rule}\n\n{text}' for url, (licence, text) in sorted(texts.items())
        )
        + '\n',
        encoding='utf-8',
    )
    write_notice(data, sorted({licence for licence, _ in texts.values()}))
    print('sources:', len(data), dict(Counter(e['licence'] for e in data)), file=sys.stderr)


def _licences(names: list[str], joiner: str) -> str:
    """'the MIT licence', 'the MIT or Apache-2.0 licences'."""
    listed = names[0] if len(names) == 1 else f'{", ".join(names[:-1])} {joiner} {names[-1]}'
    return f'the {listed} licence{"" if len(names) == 1 else "s"}'


def write_notice(sources: list[dict[str, Any]], texts: list[str]) -> None:
    """The notice beside the packs, naming only the licences SOURCES.json holds."""

    def rank(licence: str) -> tuple[int, str]:
        return (PERMISSIVE.index(licence) if licence in PERMISSIVE else len(PERMISSIVE), licence)

    taps = sorted({e['licence'] for e in sources if e['loader'] == 'singer'}, key=rank)
    kept = sorted(texts, key=rank)
    texts_of = (
        f', and ontology-pack-sources.txt for each repository under {_licences(kept, "or")}, with its '
        'copyright notice'
        if kept
        else ''
    )
    paragraphs = [
        'The generated packs in generated/ are made by .kodex/scripts/generate_packs.py from published, '
        'permissively licensed connector schemas. SOURCES.json lists every repository they are made '
        'from, with its URL, licence, commit and the packs it contributes to; the licence texts are in '
        f"the repository's licenses/ directory (Apache-2.0.txt{texts_of}). No Airbyte connector is read: "
        "Airbyte's repository is under the Elastic License 2.0.",
        "- Fivetran's dbt packages (https://github.com/fivetran, the dbt_* repositories), Copyright "
        'Fivetran Inc., licensed under the Apache License, Version 2.0. Table and column names, the '
        'tables their staging models read, the columns and values of their test seeds, and the table and '
        'column descriptions used as entity and measure definitions (from models/**/src_*.yml and the '
        'doc blocks in models/**/*.md), edited for spelling and punctuation.',
        "- dlt's verified sources (https://github.com/dlt-hub/verified-sources), by dltHub, licensed "
        'under the Apache License, Version 2.0. Resource names, primary keys and declared columns.',
    ]
    if taps:
        paragraphs.append(
            f'- Singer and Meltano taps whose repositories are licensed under {_licences(taps, "or")}, as '
            "SOURCES.json lists them with each repository's own licence. Stream names, primary keys, "
            'property names and types, and property descriptions, edited for spelling and punctuation.'
        )
    if any(e['loader'] == 'meltano hub' for e in sources):
        paragraphs.append(
            '- meltano/hub (https://github.com/meltano/hub), licensed under the Apache License, Version '
            '2.0: which source system each tap reads, and its label.'
        )
    if any(e['loader'] == 'word list' for e in sources):
        paragraphs.append(
            '- The word list of dwyl/english-words (https://github.com/dwyl/english-words), released into '
            'the public domain (the Unlicense): only to tell apart the words of a name run together, such '
            'as transactionline. No text of it is in the packs beyond those words.'
        )
    paragraphs.append('The hand-written packs in this directory are part of Konstruct (MIT).')
    NOTICE.write_text(
        '\n\n'.join(
            textwrap.fill(
                p, 100, break_on_hyphens=False, subsequent_indent='  ' if p.startswith('- ') else ''
            )
            for p in paragraphs
        )
        + '\n',
        encoding='utf-8',
    )


def write_index() -> None:
    data = registry.build_index()
    text = json.dumps(data, separators=(',', ':'), sort_keys=True) + '\n'
    registry.INDEX.write_bytes(gzip.compress(text.encode('utf-8'), compresslevel=9, mtime=0))
    words = {
        'hand-written': 'hand-written',
        'fivetran': 'Fivetran',
        'singer': 'Singer',
        'dlt': 'dlt',
    }
    rows = []
    for pack in data:
        kinds = Counter(t.get('kind') or 'term' for t in pack['terms'])
        rows.append(
            f'| `{pack["name"]}` | {pack["title"] or pack["name"]} | {kinds["entity"]} | {kinds["measure"]} | '
            f'{kinds["term"]} | {len(pack["relationships"])} | {", ".join(words.get(o, o) for o in pack["origins"])} |'
        )
    DOCS.write_text(
        '# Ontology Pack List\n\n'
        'Every ontology pack Konstruct ships, with the entities, measures, other terms and relationships '
        'it binds when a schema it recognises is in the graph, and where its tables come from. See '
        '[Ontology packs](ontology.md#ontology-packs) for how a pack is matched and bound.\n\n'
        f'{len(data)} packs.\n\n'
        '| Pack | Source | Entities | Measures | Other terms | Relationships | Tables from |\n'
        '|------|--------|---------:|---------:|------------:|--------------:|-------------|\n'
        + '\n'.join(rows)
        + '\n',
        encoding='utf-8',
    )
    print(f'index: {len(data)} packs, {registry.INDEX.stat().st_size // 1024} KB', file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--fivetran', type=Path, help="a directory holding Fivetran's dbt_* repositories")
    parser.add_argument('--taps', type=Path, help='a directory holding Singer and Meltano tap repositories')
    parser.add_argument('--hub', type=Path, help='a checkout of meltano/hub')
    parser.add_argument('--dlt', type=Path, help='a checkout of dlt-hub/verified-sources')
    parser.add_argument(
        '--words', type=Path, help='an English word list, one word a line (dwyl/english-words)'
    )
    parser.add_argument('--index', action='store_true', help='only rebuild the index and the pack list')
    args = parser.parse_args()
    if not args.index:
        if not args.fivetran:
            parser.error('--fivetran is needed, unless --index')
        if args.words:
            DICTIONARY.update(w.strip().lower() for w in args.words.read_text(encoding='utf-8').split())
        generate(args.fivetran, args.taps, args.hub, args.dlt, args.words)
    write_index()


if __name__ == '__main__':
    main()
