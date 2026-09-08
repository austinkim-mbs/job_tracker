from django.urls import path

from . import views

app_name = "interview_funnel"

urlpatterns = [
    path("chat/", views.chat_index, name="chat_index"),
    path("chat/<str:posting_id>/", views.chat_view, name="chat"),
    path("chat/<str:posting_id>/stream", views.chat_stream, name="chat_stream"),
]
