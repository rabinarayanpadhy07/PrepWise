from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework import exceptions, serializers

User = get_user_model()

EMAIL_TAKEN_MESSAGE = "An account with this email address already exists."
INVALID_CREDENTIALS_MESSAGE = "Incorrect email or password."


def normalise_email(value):
    return User.objects.normalize_email(value).strip().lower()


class UserSerializer(serializers.ModelSerializer):
    """The signed-in user's own profile. Only `full_name` is editable."""

    full_name = serializers.CharField(max_length=150, allow_blank=False, trim_whitespace=True)
    # False for accounts created with Google sign-in, which have no password to change.
    has_password = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "email", "full_name", "role", "date_joined", "has_password"]
        read_only_fields = ["id", "email", "role", "date_joined", "has_password"]

    def get_has_password(self, obj) -> bool:
        return obj.has_usable_password()


class RegisterSerializer(serializers.Serializer):
    # `role` and permission flags are deliberately absent: new users are always "user".
    email = serializers.EmailField(max_length=254)
    full_name = serializers.CharField(max_length=150, trim_whitespace=True)
    password = serializers.CharField(write_only=True, max_length=128, trim_whitespace=False)

    def validate_email(self, value):
        email = normalise_email(value)
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError(EMAIL_TAKEN_MESSAGE, code="email_taken")
        return email

    def validate(self, attrs):
        candidate = User(email=attrs["email"], full_name=attrs["full_name"])
        try:
            validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": exc.messages}) from exc
        return attrs

    def create(self, validated_data):
        try:
            with transaction.atomic():
                return User.objects.create_user(
                    email=validated_data["email"],
                    password=validated_data["password"],
                    full_name=validated_data["full_name"],
                )
        except IntegrityError as exc:
            # Two registrations for the same email raced past validate_email().
            raise serializers.ValidationError(
                {"email": [EMAIL_TAKEN_MESSAGE]}, code="email_taken"
            ) from exc


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(write_only=True, max_length=128, trim_whitespace=False)

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get("request"),
            email=normalise_email(attrs["email"]),
            password=attrs["password"],
        )
        # Inactive users and wrong passwords get the same answer.
        if user is None or not user.is_active:
            raise exceptions.AuthenticationFailed(
                INVALID_CREDENTIALS_MESSAGE, code="invalid_credentials"
            )
        attrs["user"] = user
        return attrs


class GoogleSignInSerializer(serializers.Serializer):
    # The ID token from Google Identity Services ("credential" in its callback).
    credential = serializers.CharField(max_length=4096)


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, max_length=128, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, max_length=128, trim_whitespace=False)

    def validate_current_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError(
                "Your current password is incorrect.", code="incorrect_password"
            )
        return value

    def validate(self, attrs):
        if attrs["current_password"] == attrs["new_password"]:
            raise serializers.ValidationError(
                {"new_password": ["Choose a password different from your current one."]}
            )
        _validate_new_password(attrs["new_password"], self.context["request"].user)
        return attrs


def _validate_new_password(password, user):
    try:
        validate_password(password, user=user)
    except DjangoValidationError as exc:
        raise serializers.ValidationError({"new_password": exc.messages}) from exc
