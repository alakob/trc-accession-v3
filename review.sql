-- Pending possible duplicates, newest first, with both sides side by side
CREATE OR REPLACE VIEW duplicate_review_queue AS
SELECT
  d.tender_id, d.candidate_id,
  n.accession_no  AS new_accession,  c.accession_no  AS existing_accession,
  n.source        AS new_source,     c.source        AS existing_source,
  n.title         AS new_title,      c.title         AS existing_title,
  n.buyer         AS new_buyer,      c.buyer         AS existing_buyer,
  n.deadline_at   AS new_deadline,   c.deadline_at   AS existing_deadline,
  round(d.similarity::numeric, 2)    AS title_similarity,
  round(abs(extract(epoch FROM (n.deadline_at - c.deadline_at))) / 86400, 1) AS deadline_gap_days,
  d.reason, d.created_at AS flagged_at
FROM tender_possible_duplicate d
JOIN tender n ON n.id = d.tender_id
JOIN tender c ON c.id = d.candidate_id
WHERE d.resolution IS NULL
  AND n.merged_into IS NULL AND c.merged_into IS NULL
ORDER BY d.similarity DESC, d.created_at DESC;

-- How the thresholds are performing, for tuning 0.6 / 7 days
CREATE OR REPLACE VIEW duplicate_review_stats AS
SELECT
  width_bucket(similarity, 0.6, 1.0, 4)            AS bucket,
  round(min(similarity)::numeric, 2)                AS sim_from,
  round(max(similarity)::numeric, 2)                AS sim_to,
  count(*) FILTER (WHERE resolution = 'same')       AS confirmed_same,
  count(*) FILTER (WHERE resolution = 'different')  AS confirmed_different,
  count(*) FILTER (WHERE resolution IS NULL)        AS pending,
  round(100.0 * count(*) FILTER (WHERE resolution = 'same')
        / nullif(count(*) FILTER (WHERE resolution IS NOT NULL), 0), 0) AS pct_same
FROM tender_possible_duplicate
GROUP BY 1 ORDER BY 1;

-- Mark a pair as not a duplicate
CREATE OR REPLACE FUNCTION mark_not_duplicate(p_tender uuid, p_candidate uuid)
RETURNS void LANGUAGE sql AS $$
  UPDATE tender_possible_duplicate SET resolution = 'different'
  WHERE (tender_id, candidate_id) IN ((p_tender, p_candidate), (p_candidate, p_tender));
$$;
