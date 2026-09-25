from django.contrib import admin
from django.urls import include, path

from tests.testapp import views

urlpatterns = [
    path('sealed/', include('vidlock.urls')),
    path('admin/', admin.site.urls),
    path('watch/<int:pk>/', views.page),
    path('playback/<int:pk>/', views.playback),
    path('bucket/<path:key>', views.bucket),
]
