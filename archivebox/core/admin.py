__package__ = 'archivebox.core'

from io import StringIO
from pathlib import Path
from contextlib import redirect_stdout

from django.contrib import admin
from django.urls import path
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.shortcuts import render, redirect
from django.contrib.auth import get_user_model
from django import forms

from ..util import htmldecode, urldecode, ansi_to_html

from core.models import Snapshot, ArchiveResult, Tag
from core.forms import AddLinkForm

from core.mixins import SearchResultsAdminMixin

from index.html import snapshot_icons
from logging_util import printable_filesize
from main import add, remove
from config import OUTPUT_DIR, SNAPSHOTS_PER_PAGE
from extractors import archive_links

# Admin URLs
# /admin/
# /admin/login/
# /admin/core/
# /admin/core/snapshot/
# /admin/core/snapshot/:uuid/
# /admin/core/tag/
# /admin/core/tag/:uuid/


# TODO: https://stackoverflow.com/questions/40760880/add-custom-button-to-django-admin-panel


class ArchiveResultInline(admin.TabularInline):
    model = ArchiveResult

class TagInline(admin.TabularInline):
    model = Snapshot.tags.through

from django.contrib.admin.helpers import ActionForm


class SnapshotActionForm(ActionForm):
    tag = forms.ModelChoiceField(queryset=Tag.objects.all(), required=False)


class SnapshotAdmin(SearchResultsAdminMixin, admin.ModelAdmin):
    list_display = ('added', 'title_str', 'url_str', 'files', 'size')
    sort_fields = ('title_str', 'url_str', 'added')
    readonly_fields = ('uuid', 'num_outputs', 'is_archived', 'url_hash', 'added', 'updated')
    search_fields = ('id', 'url', 'timestamp', 'title', 'tags__name')
    fields = ('timestamp', 'url', 'title', 'tags', *readonly_fields)
    list_filter = ('added', 'updated', 'tags', 'archiveresult__status')
    ordering = ['-added']
    actions = ['delete_snapshots', 'overwrite_snapshots', 'update_snapshots', 'update_titles', 'verify_snapshots', 'add_tag', 'remove_tag']
    autocomplete_fields = ['tags']
    inlines = [ArchiveResultInline]
    list_per_page = SNAPSHOTS_PER_PAGE

    action_form = SnapshotActionForm

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('grid/', self.admin_site.admin_view(self.grid_view),name='grid')
        ]
        return custom_urls + urls

    def get_queryset(self, request):
        self.request = request
        return super().get_queryset(request).prefetch_related('tags')

    def tag_list(self, obj):
        return ', '.join(obj.tags.values_list('name', flat=True))

    # TODO: figure out a different way to do this, you cant nest forms so this doenst work
    # def action(self, obj):
    #     # csrfmiddlewaretoken: Wa8UcQ4fD3FJibzxqHN3IYrrjLo4VguWynmbzzcPYoebfVUnDovon7GEMYFRgsh0
    #     # action: update_snapshots
    #     # select_across: 0
    #     # _selected_action: 76d29b26-2a88-439e-877c-a7cca1b72bb3
    #     return format_html(
    #         '''
    #             <form action="/admin/core/snapshot/" method="post" onsubmit="e => e.stopPropagation()">
    #                 <input type="hidden" name="csrfmiddlewaretoken" value="{}">
    #                 <input type="hidden" name="_selected_action" value="{}">
    #                 <button name="update_snapshots">Check</button>
    #                 <button name="update_titles">Pull title + favicon</button>
    #                 <button name="update_snapshots">Update</button>
    #                 <button name="overwrite_snapshots">Re-Archive (overwrite)</button>
    #                 <button name="delete_snapshots">Permanently delete</button>
    #             </form>
    #         ''',
    #         csrf.get_token(self.request),
    #         obj.id,
    #     )

    def uuid(self, obj):
        return format_html(
            '<code style="font-size: 10px">{}</code><br/><a href="/archive/{}">View index ➡️</a> &nbsp; &nbsp; <a href="/admin/core/snapshot/?id__exact={}">View actions ⚙️</a>',
            obj.id,
            obj.timestamp,
            obj.id,
        )

    def title_str(self, obj):
        canon = obj.as_link().canonical_outputs()
        tags = ''.join(
            format_html('<a href="/admin/core/snapshot/?tags__id__exact={}"><span class="tag">{}</span></a> ', tag.id, tag)
            for tag in obj.tags.all()
            if str(tag).strip()
        )
        return format_html(
            '<a href="/{}">'
                '<img src="/{}/{}" class="favicon" onerror="this.remove()">'
            '</a>'
            '<a href="/{}/index.html">'
                '<b class="status-{}">{}</b>'
            '</a>',
            obj.archive_path,
            obj.archive_path, canon['favicon_path'],
            obj.archive_path,
            'fetched' if obj.latest_title or obj.title else 'pending',
            urldecode(htmldecode(obj.latest_title or obj.title or ''))[:128] or 'Pending...'
        ) + mark_safe(f' <span class="tags">{tags}</span>')

    def files(self, obj):
        return snapshot_icons(obj)

    def size(self, obj):
        archive_size = (Path(obj.link_dir) / 'index.html').exists() and obj.archive_size
        if archive_size:
            size_txt = printable_filesize(archive_size)
            if archive_size > 52428800:
                size_txt = mark_safe(f'<b>{size_txt}</b>')
        else:
            size_txt = mark_safe('<span style="opacity: 0.3">...</span>')
        return format_html(
            '<a href="/{}" title="View all files">{}</a>',
            obj.archive_path,
            size_txt,
        )

    def url_str(self, obj):
        return format_html(
            '<a href="{}"><code>{}</code></a>',
            obj.url,
            obj.url.split('://www.', 1)[-1].split('://', 1)[-1][:64],
        )

    def grid_view(self, request):

        # cl = self.get_changelist_instance(request)

        # Save before monkey patching to restore for changelist list view
        saved_change_list_template = self.change_list_template
        saved_list_per_page = self.list_per_page
        saved_list_max_show_all = self.list_max_show_all

        # Monkey patch here plus core_tags.py
        self.change_list_template = 'private_index_grid.html'
        self.list_per_page = 20
        self.list_max_show_all = self.list_per_page

        # Call monkey patched view
        rendered_response = self.changelist_view(request)

        # Restore values
        self.change_list_template = saved_change_list_template
        self.list_per_page = saved_list_per_page
        self.list_max_show_all = saved_list_max_show_all

        return rendered_response


    def update_snapshots(self, request, queryset):
        archive_links([
            snapshot.as_link()
            for snapshot in queryset
        ], out_dir=OUTPUT_DIR)
    update_snapshots.short_description = "Archive"

    def update_titles(self, request, queryset):
        archive_links([
            snapshot.as_link()
            for snapshot in queryset
        ], overwrite=True, methods=('title','favicon'), out_dir=OUTPUT_DIR)
    update_titles.short_description = "Pull title"

    def overwrite_snapshots(self, request, queryset):
        archive_links([
            snapshot.as_link()
            for snapshot in queryset
        ], overwrite=True, out_dir=OUTPUT_DIR)
    overwrite_snapshots.short_description = "Re-archive (overwrite)"

    def verify_snapshots(self, request, queryset):
        for snapshot in queryset:
            print(snapshot.timestamp, snapshot.url, snapshot.is_archived, snapshot.archive_size, len(snapshot.history))

    verify_snapshots.short_description = "Check"

    def delete_snapshots(self, request, queryset):
        remove(snapshots=queryset, yes=True, delete=True, out_dir=OUTPUT_DIR)

    delete_snapshots.short_description = "Delete"

    def add_tag(self, request, queryset):
        tag = request.POST['tag']
        for obj in queryset:
            obj.tags.add(tag)

    add_tag.short_description = "Add tag"

    def remove_tag(self, request, queryset):
        tag = request.POST['tag']
        for obj in queryset:
            obj.tags.remove(tag)

    remove_tag.short_description = "Remove tag"

        

    title_str.short_description = 'Title'
    url_str.short_description = 'Original URL'

    title_str.admin_order_field = 'title'
    url_str.admin_order_field = 'url'



class TagAdmin(admin.ModelAdmin):
    list_display = ('slug', 'name', 'num_snapshots', 'snapshots', 'id')
    sort_fields = ('id', 'name', 'slug')
    readonly_fields = ('id', 'num_snapshots', 'snapshots')
    search_fields = ('id', 'name', 'slug')
    fields = (*readonly_fields, 'name', 'slug')
    actions = ['delete_selected']
    ordering = ['-id']

    def num_snapshots(self, obj):
        return format_html(
            '<a href="/admin/core/snapshot/?tags__id__exact={}">{} total</a>',
            obj.id,
            obj.snapshot_set.count(),
        )

    def snapshots(self, obj):
        total_count = obj.snapshot_set.count()
        return mark_safe('<br/>'.join(
            format_html(
                '{} <code><a href="/admin/core/snapshot/{}/change"><b>[{}]</b></a> {}</code>',
                snap.updated.strftime('%Y-%m-%d %H:%M') if snap.updated else 'pending...',
                snap.id,
                snap.timestamp,
                snap.url,
            )
            for snap in obj.snapshot_set.order_by('-updated')[:10]
        ) + (f'<br/><a href="/admin/core/snapshot/?tags__id__exact={obj.id}">and {total_count-10} more...<a>' if obj.snapshot_set.count() > 10 else ''))


class ArchiveResultAdmin(admin.ModelAdmin):
    list_display = ('id', 'start_ts', 'extractor', 'snapshot_str', 'tags_str', 'cmd_str', 'status', 'output_str')
    sort_fields = ('start_ts', 'extractor', 'status')
    readonly_fields = ('id', 'uuid', 'snapshot_str', 'tags_str')
    search_fields = ('id', 'uuid', 'snapshot__url', 'extractor', 'output', 'cmd_version', 'cmd', 'snapshot__timestamp')
    fields = (*readonly_fields, 'snapshot', 'extractor', 'status', 'start_ts', 'end_ts', 'output', 'pwd', 'cmd', 'cmd_version')
    autocomplete_fields = ['snapshot']

    list_filter = ('status', 'extractor', 'start_ts', 'cmd_version')
    ordering = ['-start_ts']
    list_per_page = SNAPSHOTS_PER_PAGE

    def snapshot_str(self, obj):
        return format_html(
            '<a href="/archive/{}/index.html"><b><code>[{}]</code></b></a><br/>'
            '<small>{}</small>',
            obj.snapshot.timestamp,
            obj.snapshot.timestamp,
            obj.snapshot.url[:128],
        )

    def tags_str(self, obj):
        return obj.snapshot.tags_str()

    def cmd_str(self, obj):
        return format_html(
            '<pre>{}</pre>',
            ' '.join(obj.cmd) if isinstance(obj.cmd, list) else str(obj.cmd),
        )

    def output_str(self, obj):
        return format_html(
            '<a href="/archive/{}/{}" class="output-link">↗️</a><pre>{}</pre>',
            obj.snapshot.timestamp,
            obj.output if (obj.status == 'succeeded') and obj.extractor not in ('title', 'archive_org') else 'index.html',
            obj.output,
        )

    tags_str.short_description = 'tags'
    snapshot_str.short_description = 'snapshot'

class ArchiveBoxAdmin(admin.AdminSite):
    site_header = 'ArchiveBox'
    index_title = 'Links'
    site_title = 'Index'

    def get_urls(self):
        return [
            path('core/snapshot/add/', self.add_view, name='Add'),
        ] + super().get_urls()

    def add_view(self, request):
        if not request.user.is_authenticated:
            return redirect(f'/admin/login/?next={request.path}')

        request.current_app = self.name
        context = {
            **self.each_context(request),
            'title': 'Add URLs',
        }

        if request.method == 'GET':
            context['form'] = AddLinkForm()

        elif request.method == 'POST':
            form = AddLinkForm(request.POST)
            if form.is_valid():
                url = form.cleaned_data["url"]
                print(f'[+] Adding URL: {url}')
                depth = 0 if form.cleaned_data["depth"] == "0" else 1
                input_kwargs = {
                    "urls": url,
                    "depth": depth,
                    "update_all": False,
                    "out_dir": OUTPUT_DIR,
                }
                add_stdout = StringIO()
                with redirect_stdout(add_stdout):
                   add(**input_kwargs)
                print(add_stdout.getvalue())

                context.update({
                    "stdout": ansi_to_html(add_stdout.getvalue().strip()),
                    "form": AddLinkForm()
                })
            else:
                context["form"] = form

        return render(template_name='add.html', request=request, context=context)

admin.site = ArchiveBoxAdmin()
admin.site.register(get_user_model())
admin.site.register(Snapshot, SnapshotAdmin)
admin.site.register(Tag, TagAdmin)
admin.site.register(ArchiveResult, ArchiveResultAdmin)
admin.site.disable_action('delete_selected')
