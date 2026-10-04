from django.contrib import admin

from .models import (Client, Debt, Installment, MobileDevice, MobileSyncBatch, Payment, Preference, SyncChange,
                     SyncConflict, SyncMutation,
                     PushDelivery, WebPushSubscription)


class InstallmentInline(admin.TabularInline):
    model = Installment
    extra = 0
    readonly_fields = ("public_id", "created_at", "updated_at")


@admin.register(Debt)
class DebtAdmin(admin.ModelAdmin):
    list_display = ("reference", "client", "loan_type", "outstanding", "status", "due_date")
    list_filter = ("loan_type", "status")
    search_fields = ("reference", "client__name", "owner__username")
    inlines = [InstallmentInline]


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "email", "address", "owner")
    search_fields = ("name", "phone", "email", "address")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("debt", "amount", "payment_type", "payment_date", "reversed_at")
    list_filter = ("payment_type",)


admin.site.register(Preference)


@admin.register(MobileDevice)
class MobileDeviceAdmin(admin.ModelAdmin):
    list_display = ("device_id", "label", "owner", "last_synced_at")
    search_fields = ("device_id", "label", "owner__username")


@admin.register(MobileSyncBatch)
class MobileSyncBatchAdmin(admin.ModelAdmin):
    list_display = ("device", "batch_id", "status", "created_at")
    search_fields = ("batch_id", "device__device_id", "device__owner__username")
    readonly_fields = ("payload_hash", "counts", "created_at", "updated_at")


class ReadOnlySyncAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SyncChange)
class SyncChangeAdmin(ReadOnlySyncAdmin):
    list_display = ("id", "entity", "entity_id", "op", "owner", "organization", "created_at")
    list_filter = ("entity", "op")
    search_fields = ("entity_id", "owner__username", "organization__name")


@admin.register(SyncMutation)
class SyncMutationAdmin(ReadOnlySyncAdmin):
    list_display = ("mutation_id", "device", "action", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("mutation_id", "device__device_id")


@admin.register(SyncConflict)
class SyncConflictAdmin(ReadOnlySyncAdmin):
    list_display = ("mutation", "entity", "entity_id", "reason", "resolved_at")
    list_filter = ("reason", "entity")


@admin.register(WebPushSubscription)
class WebPushSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("owner", "short_endpoint", "updated_at")
    search_fields = ("owner__username", "endpoint")
    readonly_fields = ("public_id", "created_at", "updated_at")

    @admin.display(description="Endpoint")
    def short_endpoint(self, obj):
        return obj.endpoint[:64]


@admin.register(PushDelivery)
class PushDeliveryAdmin(admin.ModelAdmin):
    list_display = ("event_key", "owner", "created_at")
    search_fields = ("event_key", "owner__username")
    readonly_fields = ("public_id", "created_at", "updated_at")
