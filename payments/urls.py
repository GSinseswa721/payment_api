from django.urls import path

from . import views

urlpatterns = [path("callbacks/payments", views.payment_callback)]
