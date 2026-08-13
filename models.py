"""RANKD ORM Models — mirrors rankd_schema.sql exactly"""
import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, DateTime, Boolean, Integer, DECIMAL, Text,
    ForeignKey, UniqueConstraint, CheckConstraint, Index, JSON
)
from sqlalchemy.dialects.postgresql import UUID, INET
from sqlalchemy.orm import relationship
from database import Base

def now():
    return datetime.utcnow()

class Sport(Base):
    __tablename__ = "sports"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(50), nullable=False)
    slug = Column(String(50), unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=now)

class Discipline(Base):
    __tablename__ = "disciplines"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sport_id = Column(UUID(as_uuid=True), ForeignKey("sports.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(50), nullable=False)
    slug = Column(String(50), nullable=False)
    created_at = Column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("sport_id", "slug"),)

class User(Base):
    __tablename__ = "users"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mobile_number = Column(String(20), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=True)
    password_hash = Column(String(255), nullable=True)
    otp_secret = Column(String(255), nullable=True)
    otp_verified = Column(Boolean, default=False)
    google_id = Column(String(255), unique=True, nullable=True)
    apple_id = Column(String(255), unique=True, nullable=True)
    created_at = Column(DateTime(timezone=True), default=now)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    is_venue_owner = Column(Boolean, default=False)
    pin_hash = Column(String(255), nullable=True)
    pin_set_at = Column(DateTime(timezone=True), nullable=True)
    pin_attempts = Column(Integer, default=0)
    pin_locked_until = Column(DateTime(timezone=True), nullable=True)

class PlayerProfile(Base):
    __tablename__ = "player_profiles"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    first_name = Column(String(50), nullable=False)
    last_name = Column(String(50), nullable=False)
    username = Column(String(30), unique=True, nullable=False)
    rankd_code = Column(String(10), unique=True, nullable=False)
    profile_image_url = Column(String(500), nullable=True)
    bio = Column(Text, nullable=True)
    city = Column(String(100), nullable=True)
    province = Column(String(100), nullable=True)
    country = Column(String(100), default="South Africa")
    street_address = Column(String(255), nullable=True)
    reliability_score = Column(DECIMAL(5,2), default=100.00)
    created_at = Column(DateTime(timezone=True), default=now)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)

    user = relationship("User", back_populates="profile")
    rating_profiles = relationship("RatingProfile", back_populates="player", cascade="all, delete-orphan")

User.profile = relationship("PlayerProfile", back_populates="user", uselist=False)

class RatingProfile(Base):
    __tablename__ = "rating_profiles"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id = Column(UUID(as_uuid=True), ForeignKey("player_profiles.id", ondelete="CASCADE"), nullable=False)
    discipline_id = Column(UUID(as_uuid=True), ForeignKey("disciplines.id", ondelete="CASCADE"), nullable=False)
    mu = Column(DECIMAL(10,4), default=1500.0000)
    phi = Column(DECIMAL(10,4), default=350.0000)
    volatility = Column(DECIMAL(10,6), default=0.060000)
    matches_played = Column(Integer, default=0)
    unique_opponents = Column(Integer, default=0)
    status = Column(String(20), default="UNRANKD")
    public_rating = Column(Integer, default=800)
    last_match_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__ = (UniqueConstraint("player_id", "discipline_id"),)

    player = relationship("PlayerProfile", back_populates="rating_profiles")

class Match(Base):
    __tablename__ = "matches"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sport_id = Column(UUID(as_uuid=True), ForeignKey("sports.id"), nullable=True)
    discipline_id = Column(UUID(as_uuid=True), ForeignKey("disciplines.id"), nullable=True)
    context = Column(String(20), default="RANKD_CHALLENGE")
    rating_eligible = Column(Boolean, default=True)
    format_type = Column(String(20), default="BEST_OF")
    format_value = Column(Integer, default=5)
    venue_id = Column(UUID(as_uuid=True), ForeignKey("venues.id"), nullable=True)
    scheduled_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(20), default="DRAFT")
    created_by = Column(UUID(as_uuid=True), ForeignKey("player_profiles.id"), nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)

    # NEW: Challenge negotiation fields
    waiting_on_player_id = Column(UUID(as_uuid=True), ForeignKey("player_profiles.id"), nullable=True)
    declined_by_id = Column(UUID(as_uuid=True), ForeignKey("player_profiles.id"), nullable=True)
    proposal_count = Column(Integer, default=0)

    players = relationship("MatchPlayer", back_populates="match", cascade="all, delete-orphan")
    submissions = relationship("ResultSubmission", back_populates="match", cascade="all, delete-orphan")

class MatchPlayer(Base):
    __tablename__ = "match_players"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    match_id = Column(UUID(as_uuid=True), ForeignKey("matches.id", ondelete="CASCADE"), nullable=False)
    player_id = Column(UUID(as_uuid=True), ForeignKey("player_profiles.id", ondelete="CASCADE"), nullable=False)
    player_slot = Column(Integer, nullable=False)
    is_winner = Column(Boolean, nullable=True)
    __table_args__ = (UniqueConstraint("match_id", "player_slot"),)

    match = relationship("Match", back_populates="players")

class ResultSubmission(Base):
    __tablename__ = "result_submissions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    match_id = Column(UUID(as_uuid=True), ForeignKey("matches.id", ondelete="CASCADE"), nullable=False)
    player_id = Column(UUID(as_uuid=True), ForeignKey("player_profiles.id"), nullable=False)
    player_a_score = Column(Integer, nullable=False)
    player_b_score = Column(Integer, nullable=False)
    submitted_at = Column(DateTime(timezone=True), default=now)
    revision = Column(Integer, default=1)
    __table_args__ = (UniqueConstraint("match_id", "player_id"),)

    match = relationship("Match", back_populates="submissions")

class ConfirmedResult(Base):
    __tablename__ = "confirmed_results"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    match_id = Column(UUID(as_uuid=True), ForeignKey("matches.id", ondelete="CASCADE"), unique=True, nullable=False)
    player_a_score = Column(Integer, nullable=False)
    player_b_score = Column(Integer, nullable=False)
    winner_id = Column(UUID(as_uuid=True), ForeignKey("player_profiles.id"), nullable=True)
    confirmed_at = Column(DateTime(timezone=True), default=now)
    confirmed_by_algorithm = Column(Boolean, default=False)

class RatingEvent(Base):
    __tablename__ = "rating_events"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    match_id = Column(UUID(as_uuid=True), nullable=False)
    player_id = Column(UUID(as_uuid=True), ForeignKey("player_profiles.id", ondelete="CASCADE"), nullable=False)
    rating_profile_id = Column(UUID(as_uuid=True), ForeignKey("rating_profiles.id", ondelete="CASCADE"), nullable=False)
    algorithm_version = Column(String(20), default="RANKD_V1", nullable=False)
    old_rating = Column(Integer, nullable=False)
    new_rating = Column(Integer, nullable=False)
    delta = Column(Integer, nullable=False)
    old_mu = Column(DECIMAL(10,4), nullable=False)
    new_mu = Column(DECIMAL(10,4), nullable=False)
    old_phi = Column(DECIMAL(10,4), nullable=False)
    new_phi = Column(DECIMAL(10,4), nullable=False)
    old_volatility = Column(DECIMAL(10,6), nullable=False)
    new_volatility = Column(DECIMAL(10,6), nullable=False)
    information_weight = Column(DECIMAL(5,4), nullable=False)
    integrity_weight = Column(DECIMAL(5,4), nullable=False)
    familiarity_factor = Column(DECIMAL(5,4), nullable=False)
    opponent_id = Column(UUID(as_uuid=True), ForeignKey("player_profiles.id"), nullable=True)
    score = Column(String(10), nullable=False)
    result = Column(String(1), nullable=False)
    explanation_code = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("match_id", "algorithm_version", "player_id", name="idx_rating_event_idempotency"),)

class Rivalry(Base):
    __tablename__ = "rivalries"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_a_id = Column(UUID(as_uuid=True), ForeignKey("player_profiles.id", ondelete="CASCADE"), nullable=False)
    player_b_id = Column(UUID(as_uuid=True), ForeignKey("player_profiles.id", ondelete="CASCADE"), nullable=False)
    total_matches = Column(Integer, default=0)
    player_a_wins = Column(Integer, default=0)
    player_b_wins = Column(Integer, default=0)
    draws = Column(Integer, default=0)
    total_frames_a = Column(Integer, default=0)
    total_frames_b = Column(Integer, default=0)
    current_streak_a = Column(Integer, default=0)
    current_streak_b = Column(Integer, default=0)
    longest_streak_a = Column(Integer, default=0)
    longest_streak_b = Column(Integer, default=0)
    first_match_at = Column(DateTime(timezone=True), nullable=True)
    last_match_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__ = (
        UniqueConstraint("player_a_id", "player_b_id"),
        CheckConstraint("player_a_id < player_b_id"),
    )

class Venue(Base):
    __tablename__ = "venues"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    city = Column(String(100), nullable=True)
    province = Column(String(100), nullable=True)
    country = Column(String(100), default="South Africa")
    street_address = Column(String(255), nullable=True)
    latitude = Column(DECIMAL(10,8), nullable=True)
    longitude = Column(DECIMAL(11,8), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=now)

class League(Base):
    __tablename__ = "leagues"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    city = Column(String(100), nullable=True)
    province = Column(String(100), nullable=True)
    sport_id = Column(UUID(as_uuid=True), ForeignKey("sports.id"), nullable=True)
    venue_id = Column(UUID(as_uuid=True), ForeignKey("venues.id"), nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("player_profiles.id"), nullable=True)
    is_verified = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=now)

    teams = relationship("Team", back_populates="league", cascade="all, delete-orphan")

class Team(Base):
    __tablename__ = "teams"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    league_id = Column(UUID(as_uuid=True), ForeignKey("leagues.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), nullable=False)
    created_at = Column(DateTime(timezone=True), default=now)

    league = relationship("League", back_populates="teams")

class PlayerLeague(Base):
    __tablename__ = "player_leagues"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id = Column(UUID(as_uuid=True), ForeignKey("player_profiles.id", ondelete="CASCADE"), nullable=False)
    league_id = Column(UUID(as_uuid=True), ForeignKey("leagues.id", ondelete="CASCADE"), nullable=False)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id"), nullable=True)
    division = Column(String(50), nullable=True)
    season = Column(String(20), nullable=True)
    is_verified = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("player_id", "league_id", "season"),)

class GuestInvite(Base):
    __tablename__ = "guest_invites"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    match_id = Column(UUID(as_uuid=True), ForeignKey("matches.id", ondelete="CASCADE"), nullable=False)
    created_by_player_id = Column(UUID(as_uuid=True), ForeignKey("player_profiles.id"), nullable=False)
    token_hash = Column(String(255), unique=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    claimed_at = Column(DateTime(timezone=True), nullable=True)
    claimed_by_player_id = Column(UUID(as_uuid=True), ForeignKey("player_profiles.id"), nullable=True)
    share_channel = Column(String(20), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now)

class RankdNight(Base):
    __tablename__ = "rankd_nights"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    venue_id = Column(UUID(as_uuid=True), ForeignKey("venues.id"), nullable=True)
    name = Column(String(100), nullable=True)
    scheduled_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=True)
    entry_fee = Column(String(50), default="FREE")
    max_capacity = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("player_profiles.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now)

class EventAttendance(Base):
    __tablename__ = "event_attendance"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), ForeignKey("rankd_nights.id", ondelete="CASCADE"), nullable=False)
    player_id = Column(UUID(as_uuid=True), ForeignKey("player_profiles.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(20), default="GOING")
    checked_in_at = Column(DateTime(timezone=True), nullable=True)
    is_open_to_challenges = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("event_id", "player_id"),)
