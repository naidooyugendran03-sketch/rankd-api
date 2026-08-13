"""RANKD API v1.0 — Production FastAPI Backend"""
import os
import uuid
import secrets
import hashlib
from datetime import datetime, timedelta
from typing import Optional, List
from contextlib import contextmanager

from fastapi import FastAPI, Depends, HTTPException, status, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy.orm import Session, joinedload
from jose import JWTError, jwt
from passlib.context import CryptContext

from database import engine, get_db, Base
from models import (
    User, PlayerProfile, Sport, Discipline, Venue, League, Team, PlayerLeague,
    Match, MatchPlayer, ResultSubmission, ConfirmedResult, RatingProfile,
    RatingEvent, Rivalry, GuestInvite, RankdNight, EventAttendance
)
from engine import RankdEngine, AlgorithmConfig

# ─── INIT ─────────────────────────────────────────────────────
app = FastAPI(title="RANKD API", version="1.0.0")

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "43200"))  # 30 days

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
engine_service = RankdEngine(AlgorithmConfig())

Base.metadata.create_all(bind=engine)

def seed_db():
    with contextmanager(get_db)() as db:
        if not db.query(Sport).first():
            sport = Sport(name="Pool", slug="pool")
            db.add(sport)
            db.commit()
            db.refresh(sport)
            db.add(Discipline(sport_id=sport.id, name="8-Ball", slug="8ball"))
            db.commit()
seed_db()

# ─── AUTH UTILS ───────────────────────────────────────────────
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(db: Session = Depends(get_db), token: str = Query(None, alias="token")):
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.id == uuid.UUID(user_id)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user

def get_current_player(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    profile = db.query(PlayerProfile).filter(PlayerProfile.user_id == user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Player profile not found")
    return profile

# ─── SCHEMAS ──────────────────────────────────────────────────
class SignupRequest(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=50)
    last_name: str = Field(..., min_length=1, max_length=50)
    mobile_number: str = Field(..., min_length=10, max_length=20)
    username: str = Field(..., min_length=2, max_length=30)
    city: str = Field(..., min_length=1, max_length=100)
    province: str = Field(..., min_length=1, max_length=100)
    country: str = Field(default="South Africa", max_length=100)
    street_address: Optional[str] = Field(None, max_length=255)
    otp_code: str = Field(default="000000", max_length=6)
    invite_token: Optional[str] = None

class LoginRequest(BaseModel):
    mobile_number: str
    otp_code: str = Field(default="000000")

class PlayerSearchResult(BaseModel):
    id: str
    first_name: str
    last_name: str
    username: str
    rankd_code: str
    city: Optional[str]
    province: Optional[str]
    public_rating: int
    status: str

class CreateMatchRequest(BaseModel):
    opponent_id: Optional[str] = None
    opponent_mobile: Optional[str] = None
    context: str = "RANKD_CHALLENGE"
    format_value: int = 5
    venue_id: Optional[str] = None
    proposed_datetime: Optional[datetime] = None
    rating_eligible: bool = True

class CounterProposalRequest(BaseModel):
    match_id: str
    proposed_datetime: datetime
    venue_id: Optional[str] = None

class SubmitResultRequest(BaseModel):
    match_id: str
    player_a_score: int = Field(..., ge=0, le=50)
    player_b_score: int = Field(..., ge=0, le=50)

class CreateLeagueRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    city: str
    province: str
    venue_id: Optional[str] = None
    sport_id: Optional[str] = None
    season: str = Field(default="2026")

class AddLeaguePlayerRequest(BaseModel):
    league_id: str
    player_username: str
    division: Optional[str] = None
    team_name: Optional[str] = None

class GuestInviteRequest(BaseModel):
    context: str = "RANKD_CHALLENGE"
    format_value: int = 5
    venue_id: Optional[str] = None
    proposed_datetime: Optional[datetime] = None

class WhatsAppShareRequest(BaseModel):
    player_id: str
    share_type: str = "profile"
    message_override: Optional[str] = None

class SetPinRequest(BaseModel):
    pin: str = Field(..., min_length=4, max_length=4, pattern=r"^\d{4}$")

class VerifyPinRequest(BaseModel):
    pin: str = Field(..., min_length=4, max_length=4)

# ─── PIN AUTH ─────────────────────────────────────────────────
# Common weak PINs — blocked for security
WEAK_PINS = {
    "0000", "1111", "2222", "3333", "4444", "5555", "6666", "7777", "8888", "9999",
    "1234", "4321", "1212", "6969", "1004", "2000", "2024", "2025", "2026",
    "2580", "0852", "1379", "9753", "0007", "0070", "1000", "2000", "9999"
}

@app.post("/auth/pin/set")
def set_pin(req: SetPinRequest, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    """Set a 4-digit PIN after first login."""
    user = db.query(User).filter(User.id == player.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if req.pin in WEAK_PINS:
        raise HTTPException(status_code=400, detail="That PIN is too common. Choose something only you would guess.")

    user.pin_hash = pwd_context.hash(req.pin)
    user.pin_set_at = datetime.utcnow()
    user.pin_attempts = 0
    db.commit()

    return {"success": True, "message": "PIN set. Use it to unlock RANKD on this device."}

@app.post("/auth/pin/verify")
def verify_pin(req: VerifyPinRequest, db: Session = Depends(get_db), token: str = Query(None, alias="token")):
    """Verify PIN and return fresh token."""
    if not token:
        raise HTTPException(status_code=401, detail="No token provided")

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user = db.query(User).filter(User.id == uuid.UUID(user_id)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Check if PIN is locked
    if user.pin_locked_until and user.pin_locked_until > datetime.utcnow():
        raise HTTPException(status_code=403, detail="PIN locked. Use mobile OTP to unlock.")

    if not user.pin_hash:
        raise HTTPException(status_code=400, detail="No PIN set. Use mobile OTP.")

    if not pwd_context.verify(req.pin, user.pin_hash):
        user.pin_attempts = (user.pin_attempts or 0) + 1

        # Lock after 5 failed attempts
        if user.pin_attempts >= 5:
            user.pin_locked_until = datetime.utcnow() + timedelta(minutes=30)
            db.commit()
            raise HTTPException(status_code=403, detail="Too many failed attempts. PIN locked for 30 minutes. Use mobile OTP.")

        db.commit()
        remaining = 5 - user.pin_attempts
        raise HTTPException(status_code=401, detail=f"Incorrect PIN. {remaining} attempts remaining.")

    # Success — reset attempts, issue fresh token
    user.pin_attempts = 0
    db.commit()

    fresh_token = create_access_token({"sub": str(user.id)})
    return {"token": fresh_token, "valid": True}

@app.post("/auth/pin/reset")
def reset_pin(player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    """Clear PIN so user can set a new one (after mobile OTP verification)."""
    user = db.query(User).filter(User.id == player.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.pin_hash = None
    user.pin_set_at = None
    user.pin_attempts = 0
    user.pin_locked_until = None
    db.commit()
    return {"success": True, "message": "PIN cleared. Set a new one next time you sign in."}

@app.get("/auth/pin/status")
def pin_status(player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    """Check if user has a PIN set."""
    user = db.query(User).filter(User.id == player.user_id).first()
    return {
        "has_pin": user.pin_hash is not None,
        "pin_set_at": user.pin_set_at.isoformat() if user.pin_set_at else None,
        "pin_locked": user.pin_locked_until is not None and user.pin_locked_until > datetime.utcnow()
    }

# ─── HEALTH ───────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "version": "RANKD_V1", "environment": os.getenv("ENVIRONMENT", "dev")}

# ─── AUTH ─────────────────────────────────────────────────────
@app.post("/auth/otp/send")
def send_otp(mobile_number: str, channel: str = "whatsapp", db: Session = Depends(get_db)):
    """Send OTP via WhatsApp (default) or SMS. WhatsApp is cheaper and more reliable in SA."""
    user = db.query(User).filter(User.mobile_number == mobile_number).first()

    # In production, integrate with WhatsApp Business API or Twilio WhatsApp
    # For now, mock — but structure is ready for real provider
    if channel == "whatsapp":
        message = f"🎱 Your RANKD code: 000000\n\nValid for 10 minutes. Don't share this with anyone."
        # TODO: Integrate WhatsApp Business API here
    else:
        message = "Your RANKD code is 000000. Valid for 10 minutes."
        # TODO: Integrate SMS provider here

    return {
        "sent": True,
        "message": f"OTP sent via {channel.upper()} (mock: 000000)",
        "exists": user is not None,
        "channel": channel,
        "hint": "Enter 000000 to continue"
    }

@app.post("/auth/refresh")
def refresh_token(player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    """Get a fresh token without re-entering OTP."""
    token = create_access_token({"sub": str(player.user_id)})
    return {"token": token, "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60}

@app.post("/auth/signup")
def signup(req: SignupRequest, db: Session = Depends(get_db)):
    if req.otp_code != "000000":
        raise HTTPException(status_code=400, detail="Invalid OTP")

    if db.query(User).filter(User.mobile_number == req.mobile_number).first():
        raise HTTPException(status_code=409, detail="Mobile number already registered")
    if db.query(PlayerProfile).filter(PlayerProfile.username == req.username).first():
        raise HTTPException(status_code=409, detail="Username taken")

    user = User(mobile_number=req.mobile_number, otp_verified=True)
    db.add(user)
    db.commit()
    db.refresh(user)

    rankd_code = req.username.upper()[:3] + secrets.token_hex(3).upper()[:4]
    profile = PlayerProfile(
        user_id=user.id,
        first_name=req.first_name,
        last_name=req.last_name,
        username=req.username,
        rankd_code=rankd_code,
        city=req.city,
        province=req.province,
        country=req.country,
        street_address=req.street_address
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)

    discipline = db.query(Discipline).filter(Discipline.slug == "8ball").first()
    if discipline:
        db.add(RatingProfile(
            player_id=profile.id,
            discipline_id=discipline.id,
            mu=1500.0, phi=350.0, volatility=0.06,
            status="UNRANKD", public_rating=800
        ))
        db.commit()

    match_id = None
    if req.invite_token:
        token_hash = hashlib.sha256(req.invite_token.encode()).hexdigest()
        invite = db.query(GuestInvite).filter(
            GuestInvite.token_hash == token_hash,
            GuestInvite.expires_at > datetime.utcnow(),
            GuestInvite.claimed_at == None
        ).first()

        if invite:
            match = db.query(Match).filter(Match.id == invite.match_id).first()
            if match and match.status == "INVITE_PENDING":
                db.add(MatchPlayer(match_id=match.id, player_id=profile.id, player_slot=2))
                match.status = "ACCEPTED"
                match.accepted_at = datetime.utcnow()
                invite.claimed_at = datetime.utcnow()
                invite.claimed_by_player_id = profile.id
                db.commit()
                match_id = str(match.id)

    token = create_access_token({"sub": str(user.id)})
    return {
        "token": token,
        "player": {
            "id": str(profile.id),
            "username": profile.username,
            "rankd_code": rankd_code,
            "first_name": profile.first_name,
            "status": "UNRANKD",
            "public_rating": 800,
            "placement_matches": 0,
            "placement_total": 10
        },
        "match_id": match_id
    }

@app.post("/auth/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    if req.otp_code != "000000":
        raise HTTPException(status_code=400, detail="Invalid OTP")
    user = db.query(User).filter(User.mobile_number == req.mobile_number).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    token = create_access_token({"sub": str(user.id)})
    profile = db.query(PlayerProfile).filter(PlayerProfile.user_id == user.id).first()
    rating = db.query(RatingProfile).filter(RatingProfile.player_id == profile.id).first() if profile else None
    return {
        "token": token,
        "player": {
            "id": str(profile.id),
            "username": profile.username,
            "first_name": profile.first_name,
            "status": rating.status if rating else "UNRANKD",
            "public_rating": rating.public_rating if rating else 800,
            "placement_matches": rating.matches_played if rating else 0,
            "placement_total": 10
        } if profile else None
    }

# ─── PLAYERS ──────────────────────────────────────────────────
@app.get("/players/me")
def get_me(player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    rating = db.query(RatingProfile).filter(RatingProfile.player_id == player.id).first()
    return {
        "id": str(player.id),
        "first_name": player.first_name,
        "last_name": player.last_name,
        "username": player.username,
        "rankd_code": player.rankd_code,
        "city": player.city,
        "province": player.province,
        "country": player.country,
        "street_address": player.street_address,
        "rating": rating.public_rating if rating else 800,
        "status": rating.status if rating else "UNRANKD",
        "matches_played": rating.matches_played if rating else 0,
        "placement_matches": rating.matches_played if rating else 0,
        "placement_total": 10,
        "unique_opponents": rating.unique_opponents if rating else 0,
    }

@app.get("/players/search", response_model=List[PlayerSearchResult])
def search_players(q: str = Query(..., min_length=1), db: Session = Depends(get_db)):
    search = f"%{q}%"
    results = db.query(PlayerProfile).filter(
        (PlayerProfile.first_name.ilike(search)) |
        (PlayerProfile.last_name.ilike(search)) |
        (PlayerProfile.username.ilike(search)) |
        (PlayerProfile.rankd_code.ilike(search))
    ).limit(20).all()

    out = []
    for p in results:
        rating = db.query(RatingProfile).filter(RatingProfile.player_id == p.id).first()
        out.append(PlayerSearchResult(
            id=str(p.id), first_name=p.first_name, last_name=p.last_name,
            username=p.username, rankd_code=p.rankd_code,
            city=p.city, province=p.province,
            public_rating=rating.public_rating if rating else 800,
            status=rating.status if rating else "UNRANKD"
        ))
    return out

@app.get("/players/{player_id}")
def get_player(player_id: str, db: Session = Depends(get_db)):
    p = db.query(PlayerProfile).filter(PlayerProfile.id == uuid.UUID(player_id)).first()
    if not p:
        raise HTTPException(status_code=404, detail="Player not found")
    rating = db.query(RatingProfile).filter(RatingProfile.player_id == p.id).first()
    return {
        "id": str(p.id), "first_name": p.first_name, "last_name": p.last_name,
        "username": p.username, "rankd_code": p.rankd_code,
        "city": p.city, "province": p.province,
        "rating": rating.public_rating if rating else 800,
        "status": rating.status if rating else "UNRANKD",
        "matches_played": rating.matches_played if rating else 0,
    }

# ─── LEADERBOARD & CHALLENGE ELIGIBILITY ──────────────────────
@app.get("/leaderboards/{scope}")
def get_leaderboard(scope: str, city: Optional[str] = None, db: Session = Depends(get_db)):
    """scope: 'city', 'province', 'national'"""
    q = db.query(PlayerProfile, RatingProfile).join(
        RatingProfile, PlayerProfile.id == RatingProfile.player_id
    ).filter(RatingProfile.status == "ESTABLISHED")

    if scope == "city" and city:
        q = q.filter(PlayerProfile.city.ilike(f"%{city}%"))

    results = q.order_by(RatingProfile.public_rating.desc()).limit(100).all()

    out = []
    for idx, (profile, rating) in enumerate(results, 1):
        out.append({
            "rank": idx,
            "player_id": str(profile.id),
            "name": f"{profile.first_name} {profile.last_name}",
            "username": profile.username,
            "rating": rating.public_rating,
            "city": profile.city
        })
    return {"scope": scope, "city": city, "players": out}

@app.get("/challenges/eligibility/{opponent_id}")
def check_challenge_eligibility(
    opponent_id: str,
    player: PlayerProfile = Depends(get_current_player),
    db: Session = Depends(get_db)
):
    """Check if player can challenge opponent (within 5 spots above)"""
    opponent = db.query(PlayerProfile).filter(PlayerProfile.id == uuid.UUID(opponent_id)).first()
    if not opponent:
        raise HTTPException(status_code=404, detail="Opponent not found")

    my_rating = db.query(RatingProfile).filter(RatingProfile.player_id == player.id).first()
    opp_rating = db.query(RatingProfile).filter(RatingProfile.player_id == opponent.id).first()

    if not my_rating or not opp_rating:
        raise HTTPException(status_code=400, detail="Rating data missing")

    # Get city leaderboard to check rank difference
    city = player.city or opponent.city or ""
    lb = db.query(PlayerProfile, RatingProfile).join(
        RatingProfile, PlayerProfile.id == RatingProfile.player_id
    ).filter(
        RatingProfile.status == "ESTABLISHED",
        PlayerProfile.city.ilike(f"%{city}%")
    ).order_by(RatingProfile.public_rating.desc()).all()

    my_rank = None
    opp_rank = None
    for idx, (p, r) in enumerate(lb, 1):
        if str(p.id) == str(player.id):
            my_rank = idx
        if str(p.id) == str(opponent_id):
            opp_rank = idx

    eligible = True
    reason = None
    rank_diff = None

    if my_rank and opp_rank:
        rank_diff = opp_rank - my_rank
        if rank_diff > 5:
            eligible = False
            reason = f"You can only challenge up to 5 spots above you. They are #{opp_rank}, you are #{my_rank}."

    return {
        "eligible": eligible,
        "reason": reason,
        "my_rank": my_rank,
        "opponent_rank": opp_rank,
        "rank_difference": rank_diff,
        "my_rating": my_rating.public_rating,
        "opponent_rating": opp_rating.public_rating
    }

# ─── MATCHES & CHALLENGE NEGOTIATION ──────────────────────────
@app.post("/matches")
def create_match(req: CreateMatchRequest, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    discipline = db.query(Discipline).filter(Discipline.slug == "8ball").first()

    # Check 5-spot restriction if opponent specified
    if req.opponent_id:
        opp = db.query(PlayerProfile).filter(PlayerProfile.id == uuid.UUID(req.opponent_id)).first()
        if opp:
            my_rating = db.query(RatingProfile).filter(RatingProfile.player_id == player.id).first()
            opp_rating = db.query(RatingProfile).filter(RatingProfile.player_id == opp.id).first()

            if my_rating and opp_rating and my_rating.status == "ESTABLISHED" and opp_rating.status == "ESTABLISHED":
                # Check rank difference
                city = player.city or opp.city or ""
                lb = db.query(PlayerProfile, RatingProfile).join(
                    RatingProfile, PlayerProfile.id == RatingProfile.player_id
                ).filter(
                    RatingProfile.status == "ESTABLISHED",
                    PlayerProfile.city.ilike(f"%{city}%")
                ).order_by(RatingProfile.public_rating.desc()).all()

                my_rank = None
                opp_rank = None
                for idx, (p, r) in enumerate(lb, 1):
                    if str(p.id) == str(player.id):
                        my_rank = idx
                    if str(p.id) == str(opp.id):
                        opp_rank = idx

                if my_rank and opp_rank and (opp_rank - my_rank) > 5:
                    raise HTTPException(
                        status_code=403,
                        detail=f"You can only challenge up to 5 spots above you. They are #{opp_rank}, you are #{my_rank}."
                    )

    match = Match(
        discipline_id=discipline.id if discipline else None,
        context=req.context,
        rating_eligible=req.rating_eligible and req.context != "FRIENDLY",
        format_value=req.format_value,
        venue_id=uuid.UUID(req.venue_id) if req.venue_id else None,
        scheduled_at=req.proposed_datetime,
        status="INVITE_PENDING",
        created_by=player.id,
        waiting_on_player_id=uuid.UUID(req.opponent_id) if req.opponent_id else None
    )
    db.add(match)
    db.commit()
    db.refresh(match)

    db.add(MatchPlayer(match_id=match.id, player_id=player.id, player_slot=1))

    if req.opponent_id:
        db.add(MatchPlayer(match_id=match.id, player_id=uuid.UUID(req.opponent_id), player_slot=2))

    db.commit()
    return {
        "match_id": str(match.id),
        "status": match.status,
        "share_url": f"{FRONTEND_URL}?screen=guestLanding&match={match.id}&inviter={player.username}"
    }

@app.post("/matches/{match_id}/accept")
def accept_match(match_id: str, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    match = db.query(Match).filter(Match.id == uuid.UUID(match_id)).first()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")
    if match.status not in ("INVITE_PENDING", "COUNTER_PENDING"):
        raise HTTPException(status_code=400, detail=f"Match cannot be accepted (status: {match.status})")

    existing = db.query(MatchPlayer).filter(MatchPlayer.match_id == match.id, MatchPlayer.player_id == player.id).first()
    if not existing:
        db.add(MatchPlayer(match_id=match.id, player_id=player.id, player_slot=2))

    match.status = "ACCEPTED"
    match.accepted_at = datetime.utcnow()
    match.waiting_on_player_id = None
    db.commit()
    return {"match_id": match_id, "status": match.status}

@app.post("/matches/{match_id}/decline")
def decline_match(match_id: str, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    match = db.query(Match).filter(Match.id == uuid.UUID(match_id)).first()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")
    if match.status not in ("INVITE_PENDING", "COUNTER_PENDING"):
        raise HTTPException(status_code=400, detail="Match not in negotiable state")

    match.status = "DECLINED"
    match.declined_by_id = player.id
    db.commit()
    return {"match_id": match_id, "status": "DECLINED"}

@app.post("/matches/{match_id}/counter")
def counter_proposal(match_id: str, req: CounterProposalRequest, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    match = db.query(Match).filter(Match.id == uuid.UUID(match_id)).first()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")
    if match.status not in ("INVITE_PENDING", "COUNTER_PENDING"):
        raise HTTPException(status_code=400, detail="Match not open for negotiation")

    # Update match with counter proposal
    match.scheduled_at = req.proposed_datetime
    if req.venue_id:
        match.venue_id = uuid.UUID(req.venue_id)
    match.status = "COUNTER_PENDING"
    match.proposal_count = (match.proposal_count or 0) + 1

    # Switch who is waiting
    creator = match.players[0].player_id if match.players else match.created_by
    match.waiting_on_player_id = creator if str(player.id) != str(creator) else (
        match.players[1].player_id if len(match.players) > 1 else None
    )

    db.commit()
    return {
        "match_id": match_id,
        "status": match.status,
        "proposed_datetime": req.proposed_datetime.isoformat(),
        "proposal_count": match.proposal_count,
        "waiting_on": str(match.waiting_on_player_id)
    }

@app.get("/matches/{match_id}")
def get_match(match_id: str, db: Session = Depends(get_db)):
    match = db.query(Match).filter(Match.id == uuid.UUID(match_id)).first()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    players = []
    for mp in match.players:
        p = db.query(PlayerProfile).filter(PlayerProfile.id == mp.player_id).first()
        r = db.query(RatingProfile).filter(RatingProfile.player_id == mp.player_id).first()
        players.append({
            "player_id": str(mp.player_id),
            "slot": mp.player_slot,
            "name": f"{p.first_name} {p.last_name}" if p else None,
            "username": p.username if p else None,
            "rating": r.public_rating if r else 800,
            "status": r.status if r else "UNRANKD"
        })

    venue = None
    if match.venue_id:
        v = db.query(Venue).filter(Venue.id == match.venue_id).first()
        if v:
            venue = {"id": str(v.id), "name": v.name, "city": v.city}

    return {
        "match_id": str(match.id),
        "status": match.status,
        "context": match.context,
        "format_value": match.format_value,
        "scheduled_at": match.scheduled_at.isoformat() if match.scheduled_at else None,
        "venue": venue,
        "players": players,
        "created_by": str(match.created_by),
        "waiting_on": str(match.waiting_on_player_id) if match.waiting_on_player_id else None,
        "proposal_count": match.proposal_count or 0
    }

@app.post("/matches/{match_id}/start")
def start_match(match_id: str, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    match = db.query(Match).filter(Match.id == uuid.UUID(match_id)).first()
    if not match or match.status != "ACCEPTED":
        raise HTTPException(status_code=400, detail="Match not in accepted state")
    match.status = "ACTIVE"
    db.commit()
    return {"match_id": match_id, "status": "ACTIVE"}

@app.post("/matches/{match_id}/result")
def submit_result(match_id: str, req: SubmitResultRequest, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    match = db.query(Match).filter(Match.id == uuid.UUID(match_id)).first()
    if not match or match.status not in ("ACTIVE", "AWAITING_RESULT", "RESULT_PARTIAL"):
        raise HTTPException(status_code=400, detail="Match not accepting results")

    mp = db.query(MatchPlayer).filter(MatchPlayer.match_id == match.id, MatchPlayer.player_id == player.id).first()
    if not mp:
        raise HTTPException(status_code=403, detail="You are not in this match")

    existing_sub = db.query(ResultSubmission).filter(
        ResultSubmission.match_id == match.id,
        ResultSubmission.player_id == player.id
    ).first()
    if existing_sub:
        existing_sub.player_a_score = req.player_a_score
        existing_sub.player_b_score = req.player_b_score
        existing_sub.revision += 1
    else:
        db.add(ResultSubmission(
            match_id=match.id, player_id=player.id,
            player_a_score=req.player_a_score, player_b_score=req.player_b_score
        ))

    all_subs = db.query(ResultSubmission).filter(ResultSubmission.match_id == match.id).all()
    if len(all_subs) == 1:
        match.status = "RESULT_PARTIAL"
    elif len(all_subs) >= 2:
        sub_a = [s for s in all_subs if s.player_id == match.players[0].player_id][0]
        sub_b = [s for s in all_subs if s.player_id != match.players[0].player_id][0]

        if sub_a.player_a_score == sub_b.player_a_score and sub_a.player_b_score == sub_b.player_b_score:
            match.status = "CONFIRMED"
            match.confirmed_at = datetime.utcnow()

            winner_id = None
            if req.player_a_score > req.player_b_score:
                winner_id = match.players[0].player_id
            elif req.player_b_score > req.player_a_score:
                winner_id = match.players[1].player_id

            db.add(ConfirmedResult(
                match_id=match.id,
                player_a_score=req.player_a_score,
                player_b_score=req.player_b_score,
                winner_id=winner_id,
                confirmed_by_algorithm=True
            ))

            if match.rating_eligible:
                calculate_ratings(match, req.player_a_score, req.player_b_score, db)

            update_rivalry(match, req.player_a_score, req.player_b_score, db)
        else:
            match.status = "RESULT_MISMATCH"

    db.commit()
    return {"match_id": match_id, "status": match.status}

def calculate_ratings(match: Match, score_a: int, score_b: int, db: Session):
    players = db.query(MatchPlayer).filter(MatchPlayer.match_id == match.id).order_by(MatchPlayer.player_slot).all()
    if len(players) != 2:
        return

    p1_id, p2_id = str(players[0].player_id), str(players[1].player_id)

    rp1 = db.query(RatingProfile).filter(RatingProfile.player_id == players[0].player_id).first()
    rp2 = db.query(RatingProfile).filter(RatingProfile.player_id == players[1].player_id).first()

    s1 = engine_service.register_player(p1_id, mu=float(rp1.mu), phi=float(rp1.phi))
    s2 = engine_service.register_player(p2_id, mu=float(rp2.mu), phi=float(rp2.phi))
    s1.volatility = float(rp1.volatility)
    s2.volatility = float(rp2.volatility)
    s1.matches_played = rp1.matches_played
    s2.matches_played = rp2.matches_played
    s1.unique_opponents = set()
    s2.unique_opponents = set()

    result = engine_service.calculate_match(p1_id, p2_id, score_a, score_b, match_id=str(match.id))

    rp1.mu = s1.mu; rp1.phi = s1.phi; rp1.volatility = s1.volatility
    rp1.matches_played = s1.matches_played; rp1.status = s1.status
    rp1.public_rating = s1.public_rating_display; rp1.last_match_at = datetime.utcnow()

    rp2.mu = s2.mu; rp2.phi = s2.phi; rp2.volatility = s2.volatility
    rp2.matches_played = s2.matches_played; rp2.status = s2.status
    rp2.public_rating = s2.public_rating_display; rp2.last_match_at = datetime.utcnow()

    for ev in engine_service.rating_events[-2:]:
        db.add(RatingEvent(
            match_id=uuid.UUID(ev.match_id), player_id=uuid.UUID(ev.player_id),
            rating_profile_id=rp1.id if ev.player_id == p1_id else rp2.id,
            algorithm_version=ev.algorithm_version, old_rating=ev.old_rating,
            new_rating=ev.new_rating, delta=ev.delta, old_mu=ev.old_mu,
            new_mu=ev.new_mu, old_phi=ev.old_phi, new_phi=ev.new_phi,
            old_volatility=ev.old_volatility, new_volatility=ev.new_volatility,
            information_weight=ev.information_weight, integrity_weight=ev.integrity_weight,
            familiarity_factor=ev.familiarity_factor, opponent_id=uuid.UUID(ev.opponent_id),
            score=ev.score, result=ev.result, explanation_code=ev.explanation_code
        ))
    db.commit()

def update_rivalry(match: Match, score_a: int, score_b: int, db: Session):
    players = sorted(match.players, key=lambda x: str(x.player_id))
    pa, pb = players[0].player_id, players[1].player_id

    rivalry = db.query(Rivalry).filter(
        Rivalry.player_a_id == pa, Rivalry.player_b_id == pb
    ).first()

    if not rivalry:
        rivalry = Rivalry(player_a_id=pa, player_b_id=pb, first_match_at=datetime.utcnow())
        db.add(rivalry)

    rivalry.total_matches += 1
    rivalry.last_match_at = datetime.utcnow()
    rivalry.total_frames_a += score_a
    rivalry.total_frames_b += score_b

    if score_a > score_b:
        rivalry.player_a_wins += 1
        rivalry.current_streak_a = max(1, rivalry.current_streak_a + 1) if rivalry.current_streak_a > 0 else 1
        rivalry.current_streak_b = 0
        rivalry.longest_streak_a = max(rivalry.longest_streak_a, rivalry.current_streak_a)
    elif score_b > score_a:
        rivalry.player_b_wins += 1
        rivalry.current_streak_b = max(1, rivalry.current_streak_b + 1) if rivalry.current_streak_b > 0 else 1
        rivalry.current_streak_a = 0
        rivalry.longest_streak_b = max(rivalry.longest_streak_b, rivalry.current_streak_b)
    else:
        rivalry.draws += 1

    db.commit()

# ─── NOTIFICATIONS ────────────────────────────────────────────
@app.get("/notifications")
def get_notifications(player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    """Get pending challenges and match updates for current player"""
    # Pending challenges where player is the target
    pending = db.query(Match).filter(
        Match.waiting_on_player_id == player.id,
        Match.status.in_(["INVITE_PENDING", "COUNTER_PENDING"])
    ).order_by(Match.created_at.desc()).all()

    out = []
    for m in pending:
        creator = db.query(PlayerProfile).filter(PlayerProfile.id == m.created_by).first()
        venue = db.query(Venue).filter(Venue.id == m.venue_id).first() if m.venue_id else None
        out.append({
            "match_id": str(m.id),
            "type": "challenge" if m.status == "INVITE_PENDING" else "counter",
            "from": {
                "name": f"{creator.first_name} {creator.last_name}" if creator else None,
                "username": creator.username if creator else None
            },
            "proposed_datetime": m.scheduled_at.isoformat() if m.scheduled_at else None,
            "venue": venue.name if venue else None,
            "format_value": m.format_value,
            "status": m.status,
            "proposal_count": m.proposal_count or 0
        })

    return {"notifications": out, "pending_count": len(out)}

# ─── GUEST INVITES ────────────────────────────────────────────
@app.post("/invites/guest")
def create_guest_invite(req: GuestInviteRequest, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    match = Match(
        discipline_id=db.query(Discipline).filter(Discipline.slug == "8ball").first().id,
        context=req.context,
        rating_eligible=req.context != "FRIENDLY",
        format_value=req.format_value,
        venue_id=uuid.UUID(req.venue_id) if req.venue_id else None,
        scheduled_at=req.proposed_datetime,
        status="INVITE_PENDING",
        created_by=player.id
    )
    db.add(match)
    db.commit()
    db.refresh(match)

    db.add(MatchPlayer(match_id=match.id, player_id=player.id, player_slot=1))

    token = secrets.token_urlsafe(16)
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    invite = GuestInvite(
        match_id=match.id,
        created_by_player_id=player.id,
        token_hash=token_hash,
        expires_at=datetime.utcnow() + timedelta(days=7),
        share_channel="WHATSAPP"
    )
    db.add(invite)
    db.commit()

    deep_link = f"{FRONTEND_URL}?screen=guestLanding&match={match.id}&token={token}&inviter={player.username}"
    return {
        "invite_id": str(invite.id),
        "token": token,
        "deep_link": deep_link,
        "whatsapp_url": f"https://wa.me/?text={__wa_text(player, deep_link)}"
    }

def __wa_text(player, link):
    text = f"🎱 {player.first_name} challenged you on RANKD!\n\nRanked Pool Match • Best of 5\nThink you can beat them? What's your RANKD?\n\nAccept: {link}"
    from urllib.parse import quote
    return quote(text)

@app.get("/invites/guest/claim")
def claim_guest_invite(token: str, db: Session = Depends(get_db)):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    invite = db.query(GuestInvite).filter(GuestInvite.token_hash == token_hash).first()
    if not invite or invite.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Invalid or expired invite")

    match = db.query(Match).filter(Match.id == invite.match_id).first()
    inviter = db.query(PlayerProfile).filter(PlayerProfile.id == invite.created_by_player_id).first()

    return {
        "valid": True,
        "match_id": str(match.id),
        "inviter": {"name": inviter.first_name, "username": inviter.username, "rating": 1800},
        "context": match.context,
        "format_value": match.format_value
    }

# ─── LEAGUES ──────────────────────────────────────────────────
@app.post("/leagues")
def create_league(req: CreateLeagueRequest, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == player.user_id).first()
    if not user.is_venue_owner:
        user.is_venue_owner = True

    league = League(
        name=req.name, city=req.city, province=req.province,
        venue_id=uuid.UUID(req.venue_id) if req.venue_id else None,
        sport_id=uuid.UUID(req.sport_id) if req.sport_id else db.query(Sport).filter(Sport.slug == "pool").first().id,
        created_by=player.id
    )
    db.add(league)
    db.commit()
    db.refresh(league)

    db.add(PlayerLeague(player_id=player.id, league_id=league.id, season=req.season, is_verified=True))
    db.commit()

    return {"league_id": str(league.id), "name": league.name, "invite_url": f"{FRONTEND_URL}?screen=leagueJoin&league={league.id}"}

@app.post("/leagues/{league_id}/players")
def add_league_player(league_id: str, req: AddLeaguePlayerRequest, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    league = db.query(League).filter(League.id == uuid.UUID(league_id)).first()
    if not league:
        raise HTTPException(status_code=404, detail="League not found")
    if league.created_by != player.id:
        raise HTTPException(status_code=403, detail="Only league creator can add players")

    target = db.query(PlayerProfile).filter(PlayerProfile.username == req.player_username).first()
    if not target:
        raise HTTPException(status_code=404, detail="Player not found")

    team_id = None
    if req.team_name:
        team = db.query(Team).filter(Team.league_id == league.id, Team.name == req.team_name).first()
        if not team:
            team = Team(league_id=league.id, name=req.team_name)
            db.add(team)
            db.commit()
            db.refresh(team)
        team_id = team.id

    existing = db.query(PlayerLeague).filter(
        PlayerLeague.player_id == target.id,
        PlayerLeague.league_id == league.id,
        PlayerLeague.season == req.division
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Player already in league")

    db.add(PlayerLeague(
        player_id=target.id, league_id=league.id,
        team_id=team_id, division=req.division,
        season=req.division or "2026"
    ))
    db.commit()
    return {"success": True, "player_id": str(target.id), "league_id": league_id}

@app.get("/leagues")
def list_leagues(city: Optional[str] = None, province: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(League)
    if city:
        q = q.filter(League.city.ilike(f"%{city}%"))
    if province:
        q = q.filter(League.province.ilike(f"%{province}%"))
    leagues = q.all()
    return [{"id": str(l.id), "name": l.name, "city": l.city, "province": l.province, "is_verified": l.is_verified} for l in leagues]

@app.get("/leagues/{league_id}")
def get_league(league_id: str, db: Session = Depends(get_db)):
    league = db.query(League).filter(League.id == uuid.UUID(league_id)).first()
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    players = db.query(PlayerLeague).filter(PlayerLeague.league_id == league.id).all()
    teams = db.query(Team).filter(Team.league_id == league.id).all()

    return {
        "id": str(league.id),
        "name": league.name,
        "city": league.city,
        "province": league.province,
        "is_verified": league.is_verified,
        "team_count": len(teams),
        "player_count": len(players),
        "players": [{"username": p.player.username, "division": p.division} for p in players[:50]]
    }

# ─── WHATSAPP SHARE ───────────────────────────────────────────
@app.post("/share/whatsapp")
def generate_whatsapp_share(req: WhatsAppShareRequest, db: Session = Depends(get_db)):
    player = db.query(PlayerProfile).filter(PlayerProfile.id == uuid.UUID(req.player_id)).first()
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    rating = db.query(RatingProfile).filter(RatingProfile.player_id == player.id).first()
    rankd = rating.public_rating if rating else 800

    profile_link = f"{FRONTEND_URL}?screen=playerProfile&player={player.username}"

    if req.message_override:
        text = req.message_override
    elif req.share_type == "challenge":
        text = f"🎱 {player.first_name} wants to know: What's your RANKD?\n\nChallenge me for rank. I'm rated {rankd}.\n\n{profile_link}"
    elif req.share_type == "rank_up":
        text = f"🔥 {player.first_name} just ranked up on RANKD!\n\nCurrent rating: {rankd}\n\nCan you beat me? {profile_link}"
    else:
        text = f"🎱 {player.first_name} on RANKD — Rated {rankd}.\n\nChallenge me for rank. What's your RANKD?\n\n{profile_link}"

    from urllib.parse import quote
    wa_url = f"https://wa.me/?text={quote(text)}"

    return {"whatsapp_url": wa_url, "text": text, "profile_link": profile_link}

# ─── VENUES ───────────────────────────────────────────────────
@app.get("/venues")
def list_venues(city: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(Venue)
    if city:
        q = q.filter(Venue.city.ilike(f"%{city}%"))
    venues = q.filter(Venue.is_active == True).all()
    return [{"id": str(v.id), "name": v.name, "city": v.city, "province": v.province, "address": v.street_address} for v in venues]

@app.post("/venues")
def create_venue(name: str, city: str, province: str, street_address: Optional[str] = None, db: Session = Depends(get_db), player: PlayerProfile = Depends(get_current_player)):
    venue = Venue(name=name, city=city, province=province, street_address=street_address)
    db.add(venue)
    db.commit()
    db.refresh(venue)
    return {"id": str(venue.id), "name": venue.name}

# ─── RIVALRIES ────────────────────────────────────────────────
@app.get("/players/me/rivalries")
def get_my_rivalries(db: Session = Depends(get_db), player: PlayerProfile = Depends(get_current_player)):
    rivalries = db.query(Rivalry).filter(
        (Rivalry.player_a_id == player.id) | (Rivalry.player_b_id == player.id)
    ).order_by(Rivalry.last_match_at.desc()).all()

    out = []
    for r in rivalries:
        is_a = r.player_a_id == player.id
        opp_id = r.player_b_id if is_a else r.player_a_id
        opp = db.query(PlayerProfile).filter(PlayerProfile.id == opp_id).first()
        my_wins = r.player_a_wins if is_a else r.player_b_wins
        opp_wins = r.player_b_wins if is_a else r.player_a_wins
        out.append({
            "opponent": {"id": str(opp.id), "name": f"{opp.first_name} {opp.last_name}", "username": opp.username},
            "wins": my_wins, "losses": opp_wins, "draws": r.draws,
            "total": r.total_matches, "last_played": r.last_match_at.isoformat() if r.last_match_at else None,
            "current_streak": r.current_streak_a if is_a else r.current_streak_b
        })
    return {"rivalries": out, "total_opponents": len(out)}

# ─── RUN ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
