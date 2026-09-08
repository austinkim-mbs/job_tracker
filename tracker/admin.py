from django.contrib import admin

from .models import Application, Company, Posting


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "ats_platform", "slug", "preference_rank", "last_us_engineering_posting_at", "is_invalid")
    list_editable = ("preference_rank",)
    list_filter = ("ats_platform", "is_invalid")
    search_fields = ("name", "slug")
    ordering = ("preference_rank", "name")


@admin.register(Posting)
class PostingAdmin(admin.ModelAdmin):
    list_display = ("title", "company", "location", "avg_comp", "lowest_comp", "first_seen", "last_seen")
    list_filter = ("company__ats_platform",)
    search_fields = ("title", "company__name")
    autocomplete_fields = ("company",)


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ("posting", "status", "date_applied", "avg_comp", "lowest_comp", "ats_score", "date_updated")
    list_filter = ("status",)
    search_fields = ("posting__title", "posting__company__name")
    autocomplete_fields = ("posting",)
