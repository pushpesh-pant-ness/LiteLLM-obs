-- Reporting layer: the ONLY thing Grafana reads. Clean, stable views over LiteLLM's internal
-- tables, so dashboards survive LiteLLM upgrades and every scoped role sees the same shape.
-- Idempotent; applied by service db-setup on every `up`.
--   - admin/UI calls (call_type '/...') are excluded
--   - model = the CURRENT public name of the provider route (survives renames)
--   - team / org attached; calls without a team show as '(no team: admin/UI)'
--   - no prompt / response text, no key hashes

CREATE SCHEMA IF NOT EXISTS reporting;

-- org: config/teams.yaml org (team metadata, set by provisioning), else the real LiteLLM organization
-- (Enterprise), else '(none)'.
CREATE OR REPLACE VIEW reporting.teams AS
SELECT t.team_id,
       COALESCE(t.team_alias, t.team_id)                                AS team,
       COALESCE(t.metadata->>'org', o.organization_alias, '(none)')     AS org,
       t.max_budget,
       t.budget_duration,
       t.budget_reset_at,
       t.spend                                                          AS spend_this_period,
       COALESCE((t.metadata->>'sensitive')::boolean, false)             AS sensitive,
       t.metadata->>'langfuse_project_id'                               AS langfuse_project_id
FROM "LiteLLM_TeamTable" t
LEFT JOIN "LiteLLM_OrganizationTable" o ON o.organization_id = t.organization_id;

-- Most recent public name per provider route (model_group changes when a model is renamed).
-- Successful calls only: a route can also sit behind a test / broken alias (e.g. zz-verify-failing
-- shares the mock route) whose failed calls must not rename the real model.
CREATE OR REPLACE VIEW reporting.model_names AS
SELECT DISTINCT ON (model) model AS route, model_group AS public_name
FROM "LiteLLM_SpendLogs"
WHERE model_group <> '' AND status = 'success'
  AND "startTime" > (now() AT TIME ZONE 'UTC') - interval '2 days'
ORDER BY model, "startTime" DESC;

CREATE OR REPLACE VIEW reporting.requests AS
SELECT s.request_id,
       s."startTime"                                                     AS start_time,
       NULLIF(s.team_id, '')                                             AS team_id,
       COALESCE(t.team, '(no team: admin/UI)')                           AS team,
       COALESCE(t.org, '(none)')                                         AS org,
       COALESCE(NULLIF(s.metadata->>'user_api_key_alias', ''), '(admin/UI)') AS key_alias,
       COALESCE(n.public_name, NULLIF(s.model_group, ''), s.model)       AS model,
       s.model                                                           AS route,
       s.status,
       s.metadata->'error_information'->>'error_class'                   AS error_class,
       s.spend,
       s.prompt_tokens,
       s.completion_tokens,
       s.total_tokens,
       s.request_duration_ms                                             AS duration_ms,
       NULLIF(s.session_id, '')                                          AS session_id,
       s.request_tags                                                    AS tags,
       t.langfuse_project_id
FROM "LiteLLM_SpendLogs" s
LEFT JOIN reporting.teams t       ON t.team_id = s.team_id
LEFT JOIN reporting.model_names n ON n.route = s.model
WHERE s.call_type NOT LIKE '/%';

CREATE OR REPLACE VIEW reporting.daily_team_spend AS
SELECT to_date(d.date, 'YYYY-MM-DD')                                  AS day,
       NULLIF(d.team_id, '')                                          AS team_id,
       COALESCE(t.team, '(no team: admin/UI)')                        AS team,
       COALESCE(t.org, '(none)')                                      AS org,
       CASE WHEN v.token IS NULL THEN '(deleted key)'
            ELSE COALESCE(v.key_alias, '(admin/UI)') END              AS key_alias,
       COALESCE(n.public_name, NULLIF(d.model_group, ''), d.model)    AS model,
       d.model                                                        AS route,
       d.spend,
       d.prompt_tokens,
       d.completion_tokens,
       d.api_requests                                                 AS requests,
       d.successful_requests,
       d.failed_requests
FROM "LiteLLM_DailyTeamSpend" d
LEFT JOIN reporting.teams t                 ON t.team_id = d.team_id
LEFT JOIN reporting.model_names n           ON n.route = d.model
LEFT JOIN "LiteLLM_VerificationToken" v     ON v.token = d.api_key
WHERE COALESCE(d.model, '') <> '';

-- Spend per tag per day. Admin view uses LiteLLM's long-lived daily tag table (12-month finance
-- export); scoped views rebuild the same shape from their own requests (30-day request log).
CREATE OR REPLACE VIEW reporting.daily_tag_spend AS
SELECT to_date(date, 'YYYY-MM-DD') AS day, tag, spend, api_requests AS requests,
       prompt_tokens, completion_tokens
FROM "LiteLLM_DailyTagSpend"
WHERE COALESCE(model, '') <> '';
