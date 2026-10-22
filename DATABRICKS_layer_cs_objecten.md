# Layer `hl_cs_objecten` — the object registry

A second schema alongside `hl_grondwaterpeilmeting`. Where that one holds *measurements*,
this holds the **physical objects** they belong to: where a measurement point is, who
manages it, and which water-management area it sits in.

Profiled 2026-10-20. **Six tables, 93 columns.** One example value per **column**, not
per row — two examples in the same table need not come from the same record.

> **This is what was profiled, not provably the whole schema.** "cs_objecten" reads as a
> central object registry, and a water authority's registry normally also carries pumping
> stations, weirs and culverts. Confirm with:
>
> ```sql
> SHOW TABLES IN gold_prd.hl_cs_objecten;
> ```

---

## Relevance to this pipeline, up front

| Table | Columns | Relevance |
|---|---|---|
| [`meetlocatie`](#1-meetlocatie--measurement-location-) | 36 | ⭐ **high** — coordinates, water-level area, filter depths, active flag |
| [`validatiestatus`](#4-the-validation-trio-shared-with-the-other-schema) | 2 | medium — decodes `validatie_status` everywhere |
| [`validatieresultaat`](#4-the-validation-trio-shared-with-the-other-schema) | 9 | medium — open data-quality failures, **spans schemas** |
| [`validatie_tracking`](#4-the-validation-trio-shared-with-the-other-schema) | 4 | low — per-record audit trail |
| [`rwzi`](#3-rwzi-and-zuiveringskring--wastewater-not-groundwater) | 31 | ❌ **none** — sewage treatment plants |
| [`zuiveringskring`](#3-rwzi-and-zuiveringskring--wastewater-not-groundwater) | 11 | ❌ **none** — sewage catchments |

Half this schema is wastewater infrastructure and has nothing to do with groundwater
heads. Said plainly in §3 so nobody spends an afternoon on it.

---

## 1. `meetlocatie` — measurement location ⭐

**The table worth having.** 36 columns. `meetreeks` (in `hl_grondwaterpeilmeting`) carries
`meetlocatie_code` and `meetlocatie_object_id`, which point here — see §2 for the catch.

### Identity and naming

| Column | Type | Example |
|---|---|---|
| `id` | `string` | `461c6c340e9c9305158b1d12bb4c80dc1efe537504652ae48174ed3a12ff431d` (64-hex) |
| `code` | `string` | `MPN-AS-2949` — **the readable point code** |
| `naam` | `string` | `MP H16 Waalenburg - Temperatuur water` |
| `typenaam_nen` | `string` | `Sensor` (NEN classification) |
| `obs_code` | `string` | `ONB` |
| `meetreeks_actief` | `string` | `Ja` — **"is the series active", as a Dutch string, not a boolean** |

### Location — five representations of one point

| Column | Type | Example |
|---|---|---|
| `geometrie` | `geometry(28992)` | `Geometry(b'\x01\x01…', 28992)` |
| `rd_xcoordinaat` | `decimal(38,8)` | `114994.05480000` |
| `rd_ycoordinaat` | `decimal(38,8)` | `566405.68310000` |
| `wgs84_xcoordinaat` | `decimal(38,8)` | `4.79015820` (**longitude**) |
| `wgs84_ycoordinaat` | `decimal(38,8)` | `53.08299496` (**latitude**) |
| `wkb_rds` | `binary` | `b'\x00\x00\x00\x00\x01@\xfc\x13…'` |
| `wkb_wgs84` | `string` | `POINT(4.790159034067 53.08299385028)` |

**EPSG:28992 is Amersfoort / RD New**, the Dutch national grid — metres, not degrees.
That is why `rd_*` are six-figure numbers and `wgs84_*` are degrees.

⚠️ **`x` is longitude and `y` is latitude.** Conventional for projected coordinates, and
the reverse of how most people say "lat/lon". The WKT confirms the order:
`POINT(<lon> <lat>)`.

⚠️ **The decimal columns and the WKB do not agree exactly.** `wgs84_xcoordinaat` is
`4.79015820` while `wkb_wgs84` says `4.790159034067` — they diverge at the 7th decimal,
about 0.1 mm. Harmless for mapping; it means the two were produced by different
reprojections, so **never join on a coordinate**. Join on `id` or `code`.

### Water-management area

Directly meaningful for groundwater: these codes say which managed surface-water level
the location sits against.

| Column | Type | Example |
|---|---|---|
| `peilgebiedpraktijk_code` | `string` | `8030 AP` — the practical water-level area |
| `peilafwijkingsgebied_code` | `string` | `GPG-JL-595` — level-deviation area |
| `combinatie_peilgebied_code` | `string` | `CMB_GPG-JL-595` — combined area |
| `compartiment_code` | `string` | `OW` |
| `compartiment_naam` | `string` | `Oppervlaktewater` (**surface water**) |

⚠️ **The sampled row is a surface-water temperature sensor, not a groundwater well.**
`compartiment_naam = Oppervlaktewater` and `naam` ends "Temperatuur water". So
`meetlocatie` holds **every** measurement point the authority owns, across compartments.
Filter by `compartiment_code` before assuming a row is a piezometer.

### Well construction — the same concepts as `meetreeks`

| Column | Type | Example |
|---|---|---|
| `bovenkant_filter` | `decimal(4,1)` | `NULL` |
| `onderkant_filter` | `decimal(4,1)` | `NULL` |
| `bovenkant_peilbuis` | `decimal(4,1)` | `NULL` |
| `bouwjaar` | `int` | `NULL` |

**These overlap `meetreeks`**, which has `begin_filter` / `eind_filter`
(`decimal(5,2)`) and its own `bovenkant_peilbuis` (`decimal(4,1)`). Note the filter
depths are stored at **different precision** in the two tables — `(5,2)` on `meetreeks`
versus `(4,1)` here. If both are populated they can disagree, and `meetreeks` is the more
precise. All four are NULL in this sample, consistent with a surface-water row.

### Management and ownership

| Column | Type | Example |
|---|---|---|
| `eigenaar` | `string` | `HHNK` |
| `gebiedsbeheerder_code` | `string` | `JKEIJZER` |
| `gebiedsbeheerder_naam` | `string` | `Jacob Keijzer` |
| `gebiedsbeheernummer` | `string` | `01` |
| `objectbeheerder` | `string` | `Wichard Bergen` |

> These name individual staff. Keep them out of anything published or shared outside the
> organisation.

### Mostly empty in the sample

| Column | Type | Example |
|---|---|---|
| `beheerobject_code` | `string` | `NULL` |
| `bergingsgebied_code` | `string` | `NULL` |
| `buitenwater_naam` | `string` | `NULL` |
| `functie_omschrijving` | `string` | `NULL` |
| `maximale_intensiteit` | `string` | `NULL` |
| `station_code` | `string` | `NULL` |
| `opmerking` | `string` | `NULL` |

### Housekeeping

| Column | Type | Example |
|---|---|---|
| `ingevoerd_datum_utc` | `timestamp` | `2026-03-02 13:53:52` |
| `validatie_status` | `int` | `2` |

---

## 2. ⚠️ The join to the groundwater schema is unproven

`meetreeks` carries two columns that point at this table:

| On `meetreeks` | Should match | Value in the sampled row |
|---|---|---|
| `meetlocatie_code` | `meetlocatie.code` | **`NULL`** |
| `meetlocatie_object_id` | `meetlocatie.id` | **`NULL`** |

**Both were NULL in the one `meetreeks` row that was profiled.** So the bridge between a
head series and its physical location exists in the schema but may not be populated — and
one sampled row proves nothing either way.

This decides whether the pipeline can put a series on a map, group series by
`peilgebiedpraktijk_code`, or read filter depths from here. One query settles it:

```sql
SELECT
    count(*)                                                   AS series_total,
    count(meetlocatie_code)                                    AS has_location_code,
    count(meetlocatie_object_id)                               AS has_location_id,
    round(100.0 * count(meetlocatie_object_id) / count(*), 1)   AS pct_linkable
FROM gold_prd.hl_grondwaterpeilmeting.meetreeks;
```

If `pct_linkable` is high, this gets you coordinates per series:

```sql
SELECT
    r.id            AS meetreeks_id,
    trim(r.code)    AS well_code,
    l.code          AS location_code,
    l.naam          AS location_name,
    l.compartiment_naam,
    l.peilgebiedpraktijk_code,
    l.rd_xcoordinaat, l.rd_ycoordinaat,
    l.wgs84_xcoordinaat AS lon, l.wgs84_ycoordinaat AS lat,
    r.begin_filter, r.eind_filter
FROM gold_prd.hl_grondwaterpeilmeting.meetreeks r
JOIN gold_prd.hl_cs_objecten.meetlocatie       l
  ON l.id = r.meetlocatie_object_id
WHERE r.id IN (
    SELECT meetreeks_id FROM waterkeringbeheer_prd.jvanruitenbeek.pastas_series_manifest
);
```

**If it comes back empty, fall back to `meetlocatie.code = trim(meetreeks.code)`** — the
codes may correspond by value even where the id column is unpopulated. `meetreeks.code`
has a trailing space in the sample (`'HB28-PB1 '`), so `trim()` is not optional.

---

## 3. `rwzi` and `zuiveringskring` — wastewater, not groundwater

**Neither is relevant to this pipeline.** Recorded so the next person can stop reading
here rather than working that out from the column names.

- **`rwzi`** = *rioolwaterzuiveringsinstallatie*, a **sewage treatment plant**. 31 columns.
- **`zuiveringskring`** = the **catchment a plant serves**. 11 columns.
- `zuiveringskring.rwzi_id` → `rwzi.id`.

<details>
<summary><strong><code>rwzi</code> — 31 columns</strong></summary>

| Column | Type | Example |
|---|---|---|
| `id` | `string` | `51bbdef6c6e89aca5e2c8247b29180d1f0bf421c91881bec5176d02e2b245168` |
| `object_id` | `string` | `51bbdef6c6e89aca5e2c8247b29180d1f0bf421c91881bec5176d02e2b245168` |
| `code` | `string` | `ZRW-MB-0016` |
| `naam` | `string` | `RWZI Velsen (Rijnland)` |
| `obs_code` | `string` | `ONB` |
| `geometrie` | `geometry(28992)` | `Geometry(b'\x01\x01…', 28992)` |
| `rd_xcoordinaat` | `decimal(38,8)` | `105755.52880000` |
| `rd_ycoordinaat` | `decimal(38,8)` | `497080.71480000` |
| `wgs84_xcoordinaat` | `decimal(38,8)` | `4.66266473` |
| `wgs84_ycoordinaat` | `decimal(38,8)` | `52.45926522` |
| `wkb_rds` | `binary` | `b'\x00\x00\x00\x00\x01@\xf9\xd1…'` |
| `wkb_wgs84` | `string` | `POINT(4.662665174473 52.45926520348)` |
| `adres`, `postcode`, `plaats` | `string` | `NULL` |
| `bevoegd_gezag` | `string` | `NULL` |
| `ontvangend_waterlichaam` | `string` | `NULL` |
| `debiet_waterlichaam` | `decimal(38,0)` | `NULL` |
| `effect_op_waterlichaam` | `decimal(8,2)` | `NULL` |
| `ontwerp_dagdebiet` | `decimal(38,0)` | `NULL` |
| `_vervuilingseenheid` | `decimal(38,0)` | `NULL` (leading underscore — internal/derived) |
| `jaartal_ingebruikname` | `decimal(38,0)` | `NULL` |
| `jaartal_laatste_renovatie` | `decimal(38,0)` | `NULL` |
| `binnen_straal_500m_bebouwing` | `boolean` | `NULL` |
| `stedelijk_of_landelijk` | `string` | `NULL` |
| `status_object` | `string` | `NULL` |
| `status_object_code` | `decimal(38,0)` | `NULL` |
| `z_info_code` | `string` | `NULL` |
| `opmerking` | `string` | `NULL` |
| `ingevoerd_datum_utc` | `timestamp` | `2023-07-05 09:07:07` |
| `validatie_status` | `int` | `2` |

**`id` and `object_id` carry the same value.** Either `object_id` points into a shared
object registry and happens to equal the row's own id, or it is redundant. Unresolved —
`meetreeks.object_id` behaves differently, pointing at a separate `object` table.

</details>

<details>
<summary><strong><code>zuiveringskring</code> — 11 columns</strong></summary>

| Column | Type | Example |
|---|---|---|
| `id` | `string` | `2f567aae60b0745d97daf4c35f15f9ecd8ad2f4c2c184d60b4b31695787c79a8` |
| `code` | `string` | `OPR-O-0034` |
| `naam` | `string` | `Oosthuizen` |
| `rwzi_id` | `string` | `48b2c61b8bec09409308f70aceaa2e1198fef6791345103e443760d59b4ac594` → `rwzi.id` |
| `obs_code` | `string` | `ONB` |
| `geometrie` | `geometry(28992)` | a **polygon**, not a point |
| `wkb_rds` | `binary` | `b'\x00\x00\x00\x00\x03…'` |
| `wkb_wgs84` | `string` | `POLYGON((5.017055559154 52.60311372785, …))` |
| `opmerking` | `string` | `NULL` |
| `ingevoerd_datum` | `timestamp` | `2024-04-24 23:35:49` |
| `validatie_status` | `int` | `2` |

Note `ingevoerd_datum` — **no `_utc` suffix**, unlike `meetlocatie.ingevoerd_datum_utc`
and `rwzi.ingevoerd_datum_utc`. Whether it is actually local time is unverified; treat
the timezone as unknown.

</details>

---

## 4. The validation trio, shared with the other schema

`validatie_tracking`, `validatieresultaat` and `validatiestatus` appear in **both**
`hl_cs_objecten` and `hl_grondwaterpeilmeting`, with identical shapes. A house
data-quality framework, Great-Expectations shaped.

### `validatiestatus` — decodes `validatie_status` everywhere

| Column | Type | Example |
|---|---|---|
| `id` | `bigint` | `0` |
| `omschrijving` | `string` | `Niet gevalideerd` ("not validated") |

Identical to the copy in `hl_grondwaterpeilmeting`. This is the lookup that makes
`validatie_status` readable — the integer the pipeline currently ignores.

### `validatieresultaat` — open data-quality failures

| Column | Type | Example |
|---|---|---|
| `schema` | `string` | `hl_cs_objecten` |
| `tabel` | `string` | `zuiveringskring` |
| `kolom` | `string` | `rwzi_id` |
| `verwachting_type` | `string` | `expect_column_values_to_not_be_null` |
| `validatie_type` | `string` | `schema` |
| `ernst` | `string` | `NULL` |
| `gebruikersbericht` | `string` | `Er bestaan zuiveringskringen zonder rwzi_id (verplicht veld).` |
| `pk` | `string` | `704edafe9e3a3b4e2a87430b5458ab77e9226c36f86190dcd4b8d41ef0b39f40` |
| `validatie_tijd` | `timestamp` | `2026-09-10 01:05:18.984971` |

**It carries a `schema` column, so it spans schemas.** Always filter, or you will read
another layer's failures as your own:

```sql
SELECT tabel, kolom, verwachting_type, ernst, gebruikersbericht, validatie_tijd
FROM gold_prd.hl_cs_objecten.validatieresultaat
WHERE schema = 'hl_grondwaterpeilmeting'
ORDER BY validatie_tijd DESC;
```

Two differences from the `hl_grondwaterpeilmeting` copy:

- **`ernst` is nullable.** It was `critical` there and `NULL` here, so severity cannot be
  relied on as a filter — **do not** write `WHERE ernst = 'critical'` and assume you have
  seen everything.
- **`validatie_type`** is `schema` here versus `business` there, so the framework runs at
  least two check classes.

### `validatie_tracking` — per-record audit trail

| Column | Type | Example |
|---|---|---|
| `pk` | `string` | `e6f7ede2076a467bc4a11269a4e75337045e7fa419e1c279fea97ccb198f79e1` (64-hex) |
| `record_hash` | `string` | `301e428f8294ff91110be1c7d8bd0e4c` (32 hex = **MD5**) |
| `tabel_naam` | `string` | `meetlocatie` |
| `validatie_timestamp` | `timestamp` | `2026-10-18 00:02:55.953282` |

**`pk` has no fixed format.** 64-hex here; a plain `'2'` in the
`hl_grondwaterpeilmeting` copy. It holds whatever the target table's key happens to be,
so **treat it as an opaque string** — never assume it is a hash.

`record_hash` is 32 hex = MD5, where the `id` columns are SHA-256. Two different hash
functions for two different jobs: identity versus change detection.

---

## 5. Relationships

```
meetlocatie ──< (meetreeks.meetlocatie_object_id)   ← in hl_grondwaterpeilmeting,
     │                                                 NULL in the sample — see §2
     └── peilgebiedpraktijk_code / peilafwijkingsgebied_code / combinatie_peilgebied_code
         (area codes, no FK — join by value if a peilgebied table exists)

rwzi ──< zuiveringskring.rwzi_id
rwzi.object_id == rwzi.id                           ← unexplained, see §3

validatie_status (int, on every object table) ──> validatiestatus.id
validatieresultaat / validatie_tracking: side tables, no FK into the model
```

The `id` columns follow the same convention as the other schema: **64-hex SHA-256 of the
row's natural key**, demonstrated in
[`DATABRICKS_HANDOFF.md` §2.0.5](DATABRICKS_HANDOFF.md). Worth testing here too:

```sql
SELECT id, code, sha2(code, 256) = id AS by_code
FROM gold_prd.hl_cs_objecten.meetlocatie
LIMIT 20;
```

---

## 6. Permissions

Unchanged — `gold_prd` is **read-only**. `SELECT` / `EXECUTE` / `READ VOLUME`, no
`CREATE TABLE`, no `WRITE VOLUME`. See
[`DATABRICKS_PERMISSIONS.md`](DATABRICKS_PERMISSIONS.md).

Whether the grant covers `hl_cs_objecten` as well as `hl_grondwaterpeilmeting` is
**not confirmed** — the grants were recorded per schema. Check before relying on it:

```sql
SHOW GRANTS ON SCHEMA gold_prd.hl_cs_objecten;
SELECT count(*) FROM gold_prd.hl_cs_objecten.meetlocatie;   -- cheapest real test
```

---

## 7. What to do with this

1. **Run the §2 coverage query.** Everything else depends on whether the
   `meetreeks` → `meetlocatie` link is populated.
2. **If it is**, series get a real identity: a point code, a name, coordinates, and a
   `peilgebiedpraktijk_code` to group by. That replaces `series_<hash24>` in plots and
   reports, and lets stage 3's return levels be mapped.
3. **`meetreeks_actief`** is a cheap sanity check on the manifest — a series selected for
   fitting whose location says `Nee` is worth a look.
4. **Ignore `rwzi` and `zuiveringskring`.**
