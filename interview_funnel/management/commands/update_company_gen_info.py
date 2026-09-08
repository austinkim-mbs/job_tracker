"""
Persist a company_researcher due-diligence writeup (see
interview_funnel/prompts/prep/company_researcher.md) onto the matching
tracker.Company row's gen_info field.

Only writes if gen_info is currently unset -- this command backfills, it
doesn't refresh. Pass --force to overwrite an existing writeup (e.g. the
research is materially out of date).

Run: python manage.py update_company_gen_info "Mixpanel" --json-file mixpanel_gen_info.json
     python manage.py update_company_gen_info "Merge" --json '{"overall_sentiment": "mixed", ...}'
     python manage.py update_company_gen_info "Merge" --id 27818 --json-file merge.json --force
"""

import json

from django.core.management.base import BaseCommand, CommandError

from tracker.models import Company


class Command(BaseCommand):
    help = "Write a due-diligence JSON blob into a Company's gen_info field, unless it's already set."

    def add_arguments(self, parser):
        parser.add_argument("company", help="Company name to look up (case-insensitive, exact match)")
        parser.add_argument("--id", type=int, default=None, help="Disambiguate by Company pk instead of relying on the name match")
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("--json-file", help="Path to a JSON file containing the gen_info payload")
        source.add_argument("--json", help="The gen_info payload as an inline JSON string")
        parser.add_argument("--force", action="store_true", help="Overwrite gen_info even if it's already set")

    def handle(self, *args, **options):
        company = self._resolve_company(options["company"], options["id"])

        if company.gen_info is not None and not options["force"]:
            self.stdout.write(
                f"{company.name} [{company.id}] already has gen_info set -- skipping (pass --force to overwrite)."
            )
            return

        if options["json_file"]:
            with open(options["json_file"], encoding="utf-8") as f:
                raw = f.read()
        else:
            raw = options["json"]

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CommandError(f"Invalid JSON: {e}")

        company.gen_info = payload
        company.save(update_fields=["gen_info"])
        self.stdout.write(f"Updated gen_info for {company.name} [{company.id}].")

    def _resolve_company(self, name: str, company_id: int | None) -> Company:
        if company_id is not None:
            try:
                return Company.objects.get(id=company_id)
            except Company.DoesNotExist:
                raise CommandError(f"No Company with id={company_id}")

        matches = list(Company.objects.filter(name__iexact=name))
        if not matches:
            raise CommandError(f"No Company found with name={name!r}. Pass --id to select by primary key instead.")
        if len(matches) > 1:
            listing = "\n".join(f"  id={c.id}  {c.name}  ({c.ats_platform}/{c.slug})" for c in matches)
            raise CommandError(f"Multiple companies named {name!r} -- disambiguate with --id:\n{listing}")
        return matches[0]
