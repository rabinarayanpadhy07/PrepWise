from django.urls import path

from apps.accounts import views

app_name = "accounts"

urlpatterns = [
    path("csrf/", views.CsrfTokenView.as_view(), name="csrf"),
    path("register/", views.RegisterView.as_view(), name="register"),
    path("login/", views.LoginView.as_view(), name="login"),
    path("providers/", views.AuthProvidersView.as_view(), name="providers"),
    path("google/", views.GoogleSignInView.as_view(), name="google"),
    # Admin panel sign-in: refuses anyone who isn't an administrator.
    path("admin/login/", views.AdminLoginView.as_view(), name="admin-login"),
    path("admin/google/", views.AdminGoogleSignInView.as_view(), name="admin-google"),
    path("refresh/", views.RefreshView.as_view(), name="refresh"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("me/", views.MeView.as_view(), name="me"),
    path("password/change/", views.PasswordChangeView.as_view(), name="password-change"),
]
