from django.urls import include, path

urlpatterns = [path('sealed/', include('vidlock.urls'))]
