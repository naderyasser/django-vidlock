from django.urls import path

from vidlock import views

app_name = 'vidlock'

urlpatterns = [
    path('<str:video_id>/media.m3u8', views.playlist_view, name='playlist'),
    path('<str:video_id>/key', views.key_view, name='key'),
    path('<str:video_id>/heartbeat', views.heartbeat_view, name='heartbeat'),
]
