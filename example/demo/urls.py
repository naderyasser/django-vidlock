from django.contrib import admin
from django.contrib.auth import views as auth
from django.urls import include, path
from lessons import views

urlpatterns = [
    path('', views.lesson_list, name='lessons'),
    path('lessons/<int:pk>/', views.watch, name='watch'),
    path('lessons/<int:pk>/playback/', views.playback, name='playback'),
    path('lessons/<int:pk>/report/', views.report, name='report'),
    path('login/', auth.LoginView.as_view(template_name='lessons/login.html'), name='login'),
    path('logout/', auth.LogoutView.as_view(), name='logout'),
    path('video/', include('vidlock.urls')),
    path('dev-media/', include('vidlock.contrib.devstorage')),
    path('admin/', admin.site.urls),
]
