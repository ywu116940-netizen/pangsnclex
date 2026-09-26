-- Question Bank V1. Module IDs are text in the existing modules API (module-... values).
create table if not exists public.question_bank_questions (
  id uuid primary key default gen_random_uuid(),
  module_id text not null references public.modules(id) on delete cascade,
  stem text not null,
  options jsonb not null,
  correct_index integer not null,
  correct_rationale text not null,
  stem_evidence jsonb not null,
  option_rationales jsonb not null default '[]'::jsonb,
  category text not null,
  concept text not null,
  primary_section_id text not null,
  section_ids jsonb not null default '[]'::jsonb,
  source_revision text not null,
  normalized_stem text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint question_bank_questions_correct_index_check check (correct_index >= 0 and correct_index < 8),
  constraint question_bank_questions_options_array_check check (jsonb_typeof(options) = 'array'),
  constraint question_bank_questions_stem_evidence_array_check check (jsonb_typeof(stem_evidence) = 'array'),
  constraint question_bank_questions_option_rationales_array_check check (jsonb_typeof(option_rationales) = 'array'),
  constraint question_bank_questions_section_ids_array_check check (jsonb_typeof(section_ids) = 'array'),
  constraint question_bank_questions_module_stem_unique unique (module_id, normalized_stem)
);

create index if not exists question_bank_questions_module_created_idx
  on public.question_bank_questions (module_id, created_at);
