-- Keep affiliate identifiers private to an owner while preserving idempotency.

alter table public.affiliate_events
    drop constraint if exists uq_affiliate_event_source_external;

alter table public.affiliate_events
    add constraint uq_affiliate_event_owner_source_external
    unique (user_id, source, external_event_id);
