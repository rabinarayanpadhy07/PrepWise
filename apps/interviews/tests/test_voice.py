import pytest

from apps.interviews.tests.test_interviews_api import INTERVIEWS, answer, questions
from apps.usage.models import UsageEvent

pytestmark = pytest.mark.django_db


def start_voice(client, fake_ai, count=3):
    fake_ai.respond(questions(count))
    return client.post(
        INTERVIEWS,
        {"role_title": "Support Lead", "mode": "voice", "question_count": count},
        format="json",
    )


def voice_answer(client, session_id, position, seconds, text="Spoken answer transcript here."):
    return answer(
        client, session_id, position, text=text, input_mode="voice", voice_seconds=seconds
    )


@pytest.fixture(autouse=True)
def _ai_configured(ai_config):
    """Fast mode served by OpenAI with one platform key (see conftest.ai_config)."""


def test_voice_session_prompt_asks_for_spoken_questions(auth_client, user, subscribe, fake_ai):
    subscribe(user)

    response = start_voice(auth_client, fake_ai)

    assert response.status_code == 201
    assert response.json()["mode"] == "voice"
    assert response.json()["max_voice_answer_seconds"] == 180
    assert "answer aloud" in fake_ai.calls[0]["prompt"]


def test_voice_answer_is_metered_against_subscription(auth_client, user, subscribe, fake_ai):
    subscription = subscribe(user)
    session_id = start_voice(auth_client, fake_ai).json()["id"]

    response = voice_answer(auth_client, session_id, 1, seconds=42)

    assert response.status_code == 201
    assert response.json()["input_mode"] == "voice"
    assert response.json()["voice_seconds"] == 42
    event = UsageEvent.objects.get(kind="voice_seconds")
    assert (event.quantity, event.source, event.subscription) == (42, "platform", subscription)


def test_voice_answer_retry_is_not_charged_twice(auth_client, user, subscribe, fake_ai):
    subscribe(user)
    session_id = start_voice(auth_client, fake_ai).json()["id"]

    voice_answer(auth_client, session_id, 1, seconds=30)
    voice_answer(auth_client, session_id, 1, seconds=30)

    assert UsageEvent.objects.filter(kind="voice_seconds").count() == 1


def test_voice_time_is_charged_only_up_to_the_plan_allowance(auth_client, user, subscribe, fake_ai):
    # Transcription runs in the browser and costs nothing, so once the plan's voice
    # time is used up, voice answers are still accepted; only what's left is charged.
    subscribe(user, voice_seconds_quota=40)
    session_id = start_voice(auth_client, fake_ai).json()["id"]
    assert voice_answer(auth_client, session_id, 1, seconds=30).status_code == 201

    second = voice_answer(auth_client, session_id, 2, seconds=30)
    third = voice_answer(auth_client, session_id, 3, seconds=30)

    assert (second.status_code, third.status_code) == (201, 201)
    charged = UsageEvent.objects.filter(kind="voice_seconds").values_list("quantity", flat=True)
    assert sorted(charged) == [10, 30]


def test_voice_session_starts_without_voice_time(auth_client, user, subscribe, fake_ai):
    subscribe(user, voice_seconds_quota=0)

    session_id = start_voice(auth_client, fake_ai).json()["id"]
    response = voice_answer(auth_client, session_id, 1, seconds=20)

    assert response.status_code == 201
    assert not UsageEvent.objects.filter(kind="voice_seconds").exists()


def test_free_users_can_answer_by_voice_without_being_charged(auth_client, fake_ai):
    response = auth_client.post(
        INTERVIEWS,
        {"role_title": "Support Lead", "mode": "voice", "question_count": 3},
        format="json",
    )
    assert response.status_code == 201 and response.json()["ai_source"] == "basic"

    voice = voice_answer(auth_client, response.json()["id"], 1, seconds=25)

    assert voice.status_code == 201
    assert voice.json()["voice_seconds"] == 25
    assert fake_ai.calls == []
    assert not UsageEvent.objects.exists()


def test_voice_answer_length_is_capped(auth_client, user, subscribe, fake_ai):
    subscribe(user)
    session_id = start_voice(auth_client, fake_ai).json()["id"]

    response = voice_answer(auth_client, session_id, 1, seconds=181)

    assert response.status_code == 400
    assert "voice_seconds" in response.json()["error"]["details"]


def test_voice_answer_requires_duration(auth_client, user, subscribe, fake_ai):
    subscribe(user)
    session_id = start_voice(auth_client, fake_ai).json()["id"]

    response = answer(auth_client, session_id, 1, input_mode="voice")

    assert response.status_code == 400


def test_voice_answers_are_refused_in_text_interviews(auth_client, user, subscribe, fake_ai):
    subscribe(user)
    fake_ai.respond(questions(3))
    session_id = auth_client.post(
        INTERVIEWS, {"role_title": "PM", "mode": "text", "question_count": 3}, format="json"
    ).json()["id"]

    response = voice_answer(auth_client, session_id, 1, seconds=20)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "voice_not_allowed"


def test_typed_answers_are_allowed_in_voice_interviews(auth_client, user, subscribe, fake_ai):
    subscribe(user)
    session_id = start_voice(auth_client, fake_ai).json()["id"]

    response = answer(auth_client, session_id, 1)

    assert response.status_code == 201
    assert response.json()["voice_seconds"] is None
    assert not UsageEvent.objects.filter(kind="voice_seconds").exists()
