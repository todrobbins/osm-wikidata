from flask import current_app
from collections import Counter, defaultdict
from . import match, database

import os.path
import json
import re

cat_to_ending = {}
patterns = {}
entity_types = {}
default_max_dist = 4

def get_pattern(key):
    if key in patterns:
        return patterns[key]
    return patterns.setdefault(key, re.compile(r'\b' + re.escape(key) + r'\b', re.I))

def categories_to_tags(categories, cat_to_entity=None):
    if cat_to_entity is None:
        cat_to_entity = build_cat_map()
    tags = set()
    for cat in categories:
        lc_cat = cat.lower()
        for key, value in cat_to_entity.items():
            if not get_pattern(key).search(lc_cat):
                continue
            exclude = value.get('exclude_cats')
            if exclude:
                pattern = re.compile(r'\b(' + '|'.join(re.escape(e) for e in exclude) + r')\b', re.I)
                if pattern.search(lc_cat):
                    continue
            tags |= set(value['tags'])
    return sorted(tags)

def categories_to_tags_map(categories):
    cat_to_entity = build_cat_map()
    ret = defaultdict(set)
    for cat in categories:
        lc_cat = cat.lower()
        for key, value in cat_to_entity.items():
            if not get_pattern(key).search(lc_cat):
                continue
            exclude = value.get('exclude_cats')
            if exclude:
                pattern = re.compile(r'\b(' + '|'.join(re.escape(e) for e in exclude) + r')\b', re.I)
                if pattern.search(lc_cat):
                    continue
            ret[cat] |= set(value['tags'])
    return ret

def load_entity_types():
    data_dir = current_app.config['DATA_DIR']
    filename = os.path.join(data_dir, 'entity_types.json')
    return json.load(open(filename))

def simplify_tags(tags):
    ''' remove foo=bar if dict cotains foo '''
    key_only = sorted(t for t in tags if '=' not in t)
    for k in key_only:
        for t in set(tags):
            if t.startswith(k + '='):
                tags.remove(t)
    return tags

def tag_and_key_if_possible(tags):
    ''' remove foo if dict contains foo=bar '''
    key_only = sorted(t for t in tags if '=' not in t)
    for k in key_only:
        for t in set(tags):
            if t.startswith(k + '='):
                if k in tags:
                    tags.remove(k)
                continue
    return tags

def build_cat_map():
    cat_to_entity = {}
    for i in load_entity_types():
        for c in i['cats']:
            lc_cat = c.lower()
            if ' by ' in lc_cat:
                lc_cat = lc_cat[:lc_cat.find(' by ')]
            cat_to_entity[lc_cat] = i
    return cat_to_entity

def get_ending_from_criteria(tags):
    global entity_types

    if not entity_types:
        entity_types = load_entity_types()
    tags = set(tags)

    endings = set()
    for t in entity_types:
        if tags & set(t['tags']):
            endings.update(t.get('trim', []))

    return endings

def could_be_building(tags, instanceof):
    if tags == {'place=neighbourhood', 'landuse=residential'}:
        return False  # human settlement
    if any(tag.startswith('building') for tag in tags):
        return True

    global entity_types

    if not entity_types:
        entity_types = load_entity_types()

    check_housename = False
    found_instanceof = False
    if instanceof:
        for t in entity_types:
            if t.get('wikidata') not in instanceof:
                continue
            found_instanceof = True
            if t.get('check_housename'):
                check_housename = True
    if found_instanceof:
        return check_housename

    tags = set(tags)
    return any(t.get('check_housename') and tags & set(t['tags'])
               for t in entity_types)

def get_max_dist_from_criteria(tags):
    global entity_types

    if not entity_types:
        entity_types = load_entity_types()
    tags = set(tags)

    max_dists = []
    for t in entity_types:
        type_max_dist = t.get('dist')
        if type_max_dist and tags & set(t['tags']):
            max_dists.append(type_max_dist)

    return max(max_dists) if max_dists else None

def hstore_query(tags):
    '''hstore query for use with osm2pgsql database'''
    cond = []
    for tag in tags:
        if '=' not in tag:
            cond.append(f"(tags ? '{tag}')")
            continue
        k, v = tag.split('=')
        cond.append(f"('{v}' = any(string_to_array((tags->'{k}'), ';')))")
        if '_' not in v:
            continue
        space = v.replace('_', ' ')
        cond.append(f"('{space}' = any(string_to_array((tags->'{k}'), ';')))")

    return ' or\n '.join(cond)

def nearby_nodes_sql(item, prefix, max_dist=10, limit=50):
    point = f"ST_TRANSFORM(ST_GeomFromEWKT('{item.ewkt}'), 3857)"
    sql = (f"select 'point', osm_id, name, tags, "
           f'ST_Distance({point}, way) as dist '
           f'from {prefix}_point '
           f'where ST_DWithin({point}, way, {max_dist})')
    return sql

def item_match_sql(item, prefix, ignore_tags=None, limit=50):
    point = "ST_TRANSFORM(ST_GeomFromEWKT('{}'), 3857)".format(item.ewkt)
    item_max_dist = get_max_dist_from_criteria(item.tags) or default_max_dist

    tags = item.calculate_tags(ignore_tags=ignore_tags)
    if not tags:
        return

    hstore = hstore_query(tags)
    assert hstore

    sql_list = []
    for obj_type in 'point', 'line', 'polygon':
        obj_sql = (f"select '{obj_type}', osm_id, name, tags, "
                   f'ST_Distance({point}, way) as dist '
                   f'from {prefix}_{obj_type} '
                   f'where ST_DWithin({point}, way, {item_max_dist} * 1000)')
        sql_list.append(obj_sql)
    sql = ('select * from (' + ' union '.join(sql_list) +
            f') a where ({hstore}) order by dist limit {limit}')
    return sql

def run_sql(cur, sql, debug=False):
    if debug:
        print(sql)

    cur.execute(sql)
    return cur.fetchall()

def find_nrhp_match(nrhp_numbers, rows):
    nrhp_numbers = set(nrhp_numbers)
    nrhp_match = []
    for src_type, src_id, osm_name, osm_tags, dist in rows:
        (osm_type, osm_id) = get_osm_id_and_type(src_type, src_id)

        if osm_tags.get('ref:nrhp') not in nrhp_numbers:
            continue

        candidate = {
            'osm_type': osm_type,
            'osm_id': osm_id,
            'name': osm_name,
            'tags': osm_tags,
            'dist': dist,
            'planet_table': src_type,
            'src_id': src_id,
        }
        nrhp_match.append(candidate)

    if len(nrhp_match) == 1:
        return nrhp_match

def find_matching_tags(osm, wikidata):
    matching = set()
    for wikidata_tag in wikidata:
        if '=' in wikidata_tag:
            k, _, v = wikidata_tag.partition('=')
            if k in osm and v in set(osm[k].split(';')):
                matching.add(wikidata_tag)
        elif wikidata_tag in osm:
            matching.add(wikidata_tag)
    return tag_and_key_if_possible(matching)

def bad_building_match(osm_tags, matching_tags, name_match):
    building_only_match = (matching_tags == {'building'} or
                           matching_tags == {'building=yes'})

    if not building_only_match:
        return False

    amenity = osm_tags.get('amenity', '')
    if 'parking' in amenity or 'place_of_worship' in amenity:
        return True

    if not name_match:
        return False

    for osm_key, detail_list in name_match.items():
        for match_type, value, source in detail_list:
            if not (match_type == 'both_trimmed' or
                    (osm_key == 'operator' and
                     match_type == 'wikidata_trimmed')):
                return False

    return True

def find_item_matches(cur, item, prefix, debug=False):
    if not item or not item.entity:
        return []
    wikidata_names = item.names()
    if not wikidata_names:
        return []

    cats = item.categories or []

    # point = "ST_GeomFromEWKT('{}')".format(item.ewkt)

    # item_max_dist = max(max_dist[cat] for cat in item['cats'])

    item_is_a_historic_district = item.is_a_historic_district()
    ignore_tags = {'building'} if item_is_a_historic_district else set()
    sql = item_match_sql(item, prefix, ignore_tags=ignore_tags)
    rows = run_sql(cur, sql, debug) if sql else []

    sql = nearby_nodes_sql(item, prefix)
    rows += run_sql(cur, sql, debug)
    if not rows:
        return []

    if debug:
        print('row count:', len(rows))
        print()
    seen = set()

    nrhp_numbers = item.ref_nrhp()
    if nrhp_numbers:
        found = find_nrhp_match(nrhp_numbers, rows)
        if found:
            return found

    item_identifiers = item.get_item_identifiers()

    endings = get_ending_from_criteria(item.tags)
    endings |= item.more_endings_from_isa()

    wikidata_tags = item.calculate_tags()

    candidates = []
    for osm_num, (src_type, src_id, osm_name, osm_tags, dist) in enumerate(rows):

        (osm_type, osm_id) = get_osm_id_and_type(src_type, src_id)
        if (osm_type, osm_id) in seen:
            continue
        if debug:
            print((osm_type, osm_id, osm_name, osm_tags, dist))
        seen.add((osm_type, osm_id))

        if osm_tags.get('locality') == 'townland' and 'locality=townland' not in item.tags:
            continue  # only match townlands when specifically searching for one

        if item_is_a_historic_district and 'building' in osm_tags:
            continue  # historic district shouldn't match building

        try:
            admin_level = int(osm_tags['admin_level']) if 'admin_level' in osm_tags else None
        except Exception:
            admin_level = None

        identifier_match = match.check_identifier(osm_tags, item_identifiers)

        if not identifier_match:
            if any(c.startswith('Cities ') for c in cats) and admin_level == 10:
                continue

        address_match = match.check_name_matches_address(osm_tags,
                                                         wikidata_names)

        if address_match is False:  # OSM and Wikidata addresses differ
            continue

        if (not address_match and
                match.check_for_address_in_extract(osm_tags, item.extract)):
            address_match = True

        name_match = match.check_for_match(osm_tags, wikidata_names, endings)

        if not (identifier_match or address_match or name_match):
            continue

        matching_tags = find_matching_tags(osm_tags, wikidata_tags)
        if (name_match and not identifier_match and not address_match and
                bad_building_match(osm_tags, matching_tags, name_match)):
            continue

        if (matching_tags == {'natural=peak'} and
                item.is_mountain_range and
                dist > 100):
            continue

        sql = (f'select ST_AsText(ST_Transform(way, 4326)) '
               f'from {prefix}_{src_type} '
               f'where osm_id={src_id}')
        cur.execute(sql)
        row = cur.fetchone()
        geom = row and row[0]

        candidate = {
            'osm_type': osm_type,
            'osm_id': osm_id,
            'name': osm_name,
            'tags': osm_tags,
            'dist': dist,
            # 'match': match.match_type.name,
            'planet_table': src_type,
            'src_id': src_id,
            'geom': geom,
            'identifier_match': identifier_match,
            'address_match': address_match,
            'name_match': name_match,
            'matching_tags': matching_tags,
        }
        candidates.append(candidate)
    return filter_distant(candidates)

def run_individual_match(place, item):
    conn = database.session.bind.raw_connection()
    cur = conn.cursor()

    candidates = find_item_matches(cur, item, place.prefix, debug=False)
    conn.close()

    return candidates

def get_osm_id_and_type(source_type, source_id):
    if source_type == 'point':
        return ('node', source_id)
    if source_id > 0:
        return ('way', source_id)
    return ('relation', -source_id)

def planet_table_id(osm):
    osm_id = int(osm['id'])
    if osm['type'] == 'node':
        return ('point', osm_id)
    table = 'polygon' if 'way_area' in osm['tags'] else 'line'
    return (table, osm_id if osm['type'] == 'way' else -osm_id)

def get_biggest_polygon(item):
    biggest = None
    biggest_size = None
    for osm in item['candidates']:
        if osm['type'] not in {'way', 'relation'}:
            continue
        if 'way_area' not in osm['tags']:
            continue
        area = float(osm['tags']['way_area'])
        if biggest is None or area > biggest_size:
            biggest_size = area
            biggest = osm

    return -osm['id'] if osm['type'] == 'relation' else osm['id']

def all_in_one(item, conn, prefix):
    cur = conn.cursor()
    biggest = get_biggest_polygon(item)
    if not biggest:
        return
    sql_list = []

    for table in 'point', 'line', 'polygon':
        id_list = ','.join(str(osm['src_id']) for osm in item['candidates']
                       if osm['table'] == table and (table == 'point' or osm['src_id'] != biggest))

        if not id_list:
            continue
        obj_sql = ('select \'{}\' as t, osm_id, way '
                   'from {}_{} '
                   'where osm_id in ({})').format(table, table, prefix, id_list)
        sql_list.append(obj_sql)

    if not sql_list:
        return
    sql = 'select ST_Within(a.way, b.way) from (' + ' union '.join(sql_list) + ') a, {}_polygon b where b.osm_id={}'.format(prefix, biggest)
    cur.execute(sql)
    if all(row[0] for row in cur.fetchall()):
        return biggest

def filter_candidates(items, conn):  # unused?
    assert isinstance(items, list)
    for item in items[:]:
        candidates = item['candidates']
        for osm in candidates:
            osm['table'], osm['src_id'] = planet_table_id(osm)

        for line in candidates[:]:
            if line['table'] == 'line':
                if any(poly['table'] == 'polygon' and poly['src_id'] == line['src_id'] for poly in candidates):
                    candidates.remove(line)

        if not candidates:
            items.remove(item)
            continue

        item['candidates'] = candidates
        if len(candidates) == 1:
            continue

        re_place_cat = re.compile(r'\b(Districts|Areas|Cities|Towns|Villages|Airports)\b', re.I)
        if any(re_place_cat.search(cat) for cat in item['cats']):
            nodes = [osm for osm in candidates if osm['type'] == 'node']
            if len(nodes) == 1:
                candidates = nodes

        if len(candidates) == 1:
            item['candidates'] = candidates
            continue

        big = all_in_one(item, conn)
        if big:
            for osm in candidates:
                if osm['table'] == 'polygon' and osm['src_id'] == big:
                    candidates = [osm]
                    break
        if len(candidates) == 1:
            item['candidates'] = candidates
            continue
    return items

def filter_place(candidates):
    # FIXME: some places are more complex, Cambridge for example
    types = {c.osm_type for c in candidates}
    place_node = None
    other = False
    if len(candidates) < 2 or 'node' not in types:
        return
    if len(types) < 2:
        return

    for c in candidates:
        if c.osm_type == 'node':
            if 'place' in c.tags:
                place_node = c
        else:
            if 'admin_level' in c.tags or c.tags.get('landuse') == 'residential':
                other = True

    if place_node and other:
        return place_node

def filter_schools(candidates):
    if len(candidates) < 2:
        return
    if all('amenity=school' not in c.matching_tags() for c in candidates):
        return

    # use the one thing tagged amenity=school
    # check everything else is tagged building=school

    match = None
    for c in candidates:
        tags = c.matching_tags()
        if 'amenity=school' in tags:
            if match:
                return
            match = c
        elif tags != ['building=school']:
            return
    return match

def filter_churches(candidates):
    if len(candidates) < 2:
        return
    if all('amenity=place_of_worship' not in c.matching_tags() for c in candidates):
        return

    # use the one thing tagged amenity=place_of_worship
    # check everything else is tagged religion=christian

    match = None
    for c in candidates:
        tags = c.matching_tags()
        if 'amenity=place_of_worship' in tags:
            if match:
                return
            match = c
        elif tags != ['religion=christian']:
            return
    return match

def filter_station(candidates):
    if len(candidates) < 2:
        return
    if all('public_transport=station' not in c.matching_tags() for c in candidates):
        return

    # use the one thing tagged public_transport=station
    # check everything else is tagged public_transport=platform

    match = None
    for c in candidates:
        tags = c.matching_tags()
        if 'public_transport=station' in tags:
            if match:  # multiple stations
                return
            match = c
        elif 'railway=tram_stop' not in tags:
            return
    return match

def filter_candidates_more(items, bad=None):
    osm_count = Counter()

    if bad is None:
        bad = {}

    for item in items:
        for c in item.candidates:
            osm_count[(c.osm_type, c.osm_id)] += 1

    for item in items:
        if item.item_id in bad:
            yield (item, {'note': 'has bad match'})
            continue
        candidates = item.candidates.all()

        done = False
        for candidate in candidates:
            housename = candidate.tags.get('addr:housename')
            if housename and housename.isdigit():
                yield (item, {'note': 'number as house name'})
                done = True
                break
            name = candidate.tags.get('name')
            if name and name.isdigit():
                yield (item, {'note': 'number as name'})
                done = True
                break
        if done:
            continue

        # place = filter_place(candidates)
        # if place:
        #     candidates = [place]
        # else:
        school = filter_schools(candidates)
        if school:
            candidates = [school]

        station = filter_station(candidates)
        if station:
            candidates = [station]

        church = filter_churches(candidates)
        if church:
            candidates = [church]

        if len(candidates) != 1:
            yield (item, {'note': 'more than one candidate found'})
            continue

        candidate = candidates[0]

        if osm_count[(candidate.osm_type, candidate.osm_id)] > 1:
            yield (item, {'note': 'OSM candidate matches multiple Wikidata items'})
            continue

        if 'wikidata' in candidate.tags:
            yield (item, {'note': 'candidate already tagged'})
            continue

        yield (item, {'candidate': candidate})

def filter_distant(candidates):
    if any(c['tags'].keys() & {'place', 'admin_level'} for c in candidates):
        return candidates
    if len(candidates) < 2:
        return candidates

    chosen = None
    for c in candidates:
        if c['dist'] < 50:
            if chosen:
                return candidates
            chosen = c
            continue
        if c['dist'] < 1000:
            return candidates
    return [chosen] if chosen else candidates
