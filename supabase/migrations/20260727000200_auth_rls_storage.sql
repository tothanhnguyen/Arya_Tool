-- Supabase Auth mapping, row-level security, and private Storage buckets.

create or replace function public.current_app_user_id()
returns bigint
language sql
stable
security definer
set search_path = ''
as $$
    select u.id
    from public.users as u
    where u.auth_user_id = auth.uid()
    limit 1
$$;

revoke all on function public.current_app_user_id() from public;
grant execute on function public.current_app_user_id() to authenticated, service_role;

create or replace function public.handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
    insert into public.users (auth_user_id, profile_json)
    values (
        new.id,
        jsonb_build_object(
            'source', 'supabase_auth',
            'email', coalesce(new.email, '')
        )
    )
    on conflict (auth_user_id) do nothing;
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
after insert on auth.users
for each row execute procedure public.handle_new_auth_user();

alter table public.users enable row level security;
alter table public.conversations enable row level security;
alter table public.messages enable row level security;
alter table public.tasks enable row level security;
alter table public.steps enable row level security;
alter table public.llm_calls enable row level security;
alter table public.scheduled_jobs enable row level security;
alter table public.notes enable row level security;
alter table public.todos enable row level security;
alter table public.social_accounts enable row level security;
alter table public.media_assets enable row level security;
alter table public.affiliate_products enable row level security;
alter table public.social_posts enable row level security;
alter table public.content_generations enable row level security;
alter table public.publish_jobs enable row level security;
alter table public.publish_attempts enable row level security;
alter table public.affiliate_events enable row level security;

revoke all on all tables in schema public from anon;
revoke all on all sequences in schema public from anon;
grant usage on schema public to authenticated;
grant select, insert, update, delete on all tables in schema public to authenticated;
grant usage, select on all sequences in schema public to authenticated;

create policy users_select_own on public.users
for select to authenticated
using (id = public.current_app_user_id());

create policy users_update_own on public.users
for update to authenticated
using (id = public.current_app_user_id())
with check (id = public.current_app_user_id());

create policy conversations_owner_all on public.conversations
for all to authenticated
using (user_id = public.current_app_user_id())
with check (user_id = public.current_app_user_id());

create policy messages_owner_all on public.messages
for all to authenticated
using (
    exists (
        select 1
        from public.conversations as c
        where c.id = messages.conversation_id
          and c.user_id = public.current_app_user_id()
    )
)
with check (
    exists (
        select 1
        from public.conversations as c
        where c.id = messages.conversation_id
          and c.user_id = public.current_app_user_id()
    )
);

create policy tasks_owner_all on public.tasks
for all to authenticated
using (user_id = public.current_app_user_id())
with check (user_id = public.current_app_user_id());

create policy steps_owner_all on public.steps
for all to authenticated
using (
    exists (
        select 1
        from public.tasks as t
        where t.id = steps.task_id
          and t.user_id = public.current_app_user_id()
    )
)
with check (
    exists (
        select 1
        from public.tasks as t
        where t.id = steps.task_id
          and t.user_id = public.current_app_user_id()
    )
);

create policy llm_calls_owner_all on public.llm_calls
for all to authenticated
using (
    exists (
        select 1
        from public.tasks as t
        where t.id = llm_calls.task_id
          and t.user_id = public.current_app_user_id()
    )
    or exists (
        select 1
        from public.steps as s
        join public.tasks as t on t.id = s.task_id
        where s.id = llm_calls.step_id
          and t.user_id = public.current_app_user_id()
    )
)
with check (
    exists (
        select 1
        from public.tasks as t
        where t.id = llm_calls.task_id
          and t.user_id = public.current_app_user_id()
    )
    or exists (
        select 1
        from public.steps as s
        join public.tasks as t on t.id = s.task_id
        where s.id = llm_calls.step_id
          and t.user_id = public.current_app_user_id()
    )
);

create policy scheduled_jobs_owner_all on public.scheduled_jobs
for all to authenticated
using (user_id = public.current_app_user_id())
with check (user_id = public.current_app_user_id());

create policy notes_owner_all on public.notes
for all to authenticated
using (user_id = public.current_app_user_id())
with check (user_id = public.current_app_user_id());

create policy todos_owner_all on public.todos
for all to authenticated
using (user_id = public.current_app_user_id())
with check (user_id = public.current_app_user_id());

create policy social_accounts_owner_all on public.social_accounts
for all to authenticated
using (user_id = public.current_app_user_id())
with check (user_id = public.current_app_user_id());

create policy media_assets_owner_all on public.media_assets
for all to authenticated
using (user_id = public.current_app_user_id())
with check (user_id = public.current_app_user_id());

create policy affiliate_products_owner_all on public.affiliate_products
for all to authenticated
using (user_id = public.current_app_user_id())
with check (user_id = public.current_app_user_id());

create policy social_posts_owner_all on public.social_posts
for all to authenticated
using (user_id = public.current_app_user_id())
with check (user_id = public.current_app_user_id());

create policy content_generations_owner_all on public.content_generations
for all to authenticated
using (
    exists (
        select 1
        from public.social_posts as p
        where p.id = content_generations.social_post_id
          and p.user_id = public.current_app_user_id()
    )
)
with check (
    exists (
        select 1
        from public.social_posts as p
        where p.id = content_generations.social_post_id
          and p.user_id = public.current_app_user_id()
    )
);

create policy publish_jobs_owner_all on public.publish_jobs
for all to authenticated
using (
    exists (
        select 1
        from public.social_posts as p
        where p.id = publish_jobs.social_post_id
          and p.user_id = public.current_app_user_id()
    )
)
with check (
    exists (
        select 1
        from public.social_posts as p
        join public.social_accounts as a
          on a.id = publish_jobs.social_account_id
        where p.id = publish_jobs.social_post_id
          and p.user_id = public.current_app_user_id()
          and a.user_id = public.current_app_user_id()
    )
);

create policy publish_attempts_owner_all on public.publish_attempts
for all to authenticated
using (
    exists (
        select 1
        from public.publish_jobs as j
        join public.social_posts as p on p.id = j.social_post_id
        where j.id = publish_attempts.publish_job_id
          and p.user_id = public.current_app_user_id()
    )
)
with check (
    exists (
        select 1
        from public.publish_jobs as j
        join public.social_posts as p on p.id = j.social_post_id
        where j.id = publish_attempts.publish_job_id
          and p.user_id = public.current_app_user_id()
    )
);

create policy affiliate_events_owner_all on public.affiliate_events
for all to authenticated
using (user_id = public.current_app_user_id())
with check (user_id = public.current_app_user_id());

insert into storage.buckets (
    id,
    name,
    public,
    file_size_limit,
    allowed_mime_types
)
values
    (
        'arya-media',
        'arya-media',
        false,
        52428800,
        array['image/jpeg', 'image/png', 'image/webp', 'video/mp4']
    ),
    (
        'arya-artifacts',
        'arya-artifacts',
        false,
        52428800,
        array[
            'text/csv',
            'text/markdown',
            'application/json',
            'application/zip',
            'application/octet-stream'
        ]
    )
on conflict (id) do update
set
    public = excluded.public,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

create policy arya_private_objects_select on storage.objects
for select to authenticated
using (
    bucket_id in ('arya-media', 'arya-artifacts')
    and (storage.foldername(name))[1] = 'users'
    and (storage.foldername(name))[2] = public.current_app_user_id()::text
);

create policy arya_private_objects_insert on storage.objects
for insert to authenticated
with check (
    bucket_id in ('arya-media', 'arya-artifacts')
    and (storage.foldername(name))[1] = 'users'
    and (storage.foldername(name))[2] = public.current_app_user_id()::text
);

create policy arya_private_objects_update on storage.objects
for update to authenticated
using (
    bucket_id in ('arya-media', 'arya-artifacts')
    and (storage.foldername(name))[1] = 'users'
    and (storage.foldername(name))[2] = public.current_app_user_id()::text
)
with check (
    bucket_id in ('arya-media', 'arya-artifacts')
    and (storage.foldername(name))[1] = 'users'
    and (storage.foldername(name))[2] = public.current_app_user_id()::text
);

create policy arya_private_objects_delete on storage.objects
for delete to authenticated
using (
    bucket_id in ('arya-media', 'arya-artifacts')
    and (storage.foldername(name))[1] = 'users'
    and (storage.foldername(name))[2] = public.current_app_user_id()::text
);
