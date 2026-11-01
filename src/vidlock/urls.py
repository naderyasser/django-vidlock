from django.urls import path

from vidlock import views

app_name = 'vidlock'

urlpatterns = [
    path('upload', views.upload_view, name='upload'),
    path('<str:video_id>/media.m3u8', views.playlist_view, name='playlist'),
    path('<str:video_id>/key', views.key_view, name='key'),
    path('<str:video_id>/heartbeat', views.heartbeat_view, name='heartbeat'),
    path('<str:video_id>/captions.vtt', views.captions_view, name='captions'),
    path('<str:video_id>/poster.jpg', views.poster_view, name='poster'),
    path('<str:video_id>/thumbnail.jpg', views.thumbnail_view, name='thumbnail'),
    path('<str:video_id>/thumbnails.vtt', views.thumbnails_view, name='thumbnails'),
]
