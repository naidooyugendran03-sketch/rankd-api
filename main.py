"""RANKD API v1.2 — Live Match Flow Update"""
import os
import uuid
import secrets
import hashlib
from datetime import datetime, timedelta
from typing import Optional, List
from contextlib import contextmanager

from fastapi import FastAPI, Depends, HTTPException, status, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy.orm import Session, joinedload
from jose import JWTError, jwt
from passlib.context import CryptContext

from database import engine, get_db, Base
from models import (
    User, PlayerProfile, Sport, Discipline, Venue, League, Team, PlayerLeague,
    Match, MatchPlayer, MatchRack, ResultSubmission, ConfirmedResult, RatingProfile,
    RatingEvent, Rivalry, GuestInvite, RankdNight, EventAttendance
)
from engine import RankdEngine, AlgorithmConfig

# ─── INIT ─────────────────────────────────────────────────────
app = FastAPI(title="RANKD API", version="1.2.0")

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "43200"))  # 30 days

# ─── CORS FIX ─────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
engine_service = RankdEngine(AlgorithmConfig())

Base.metadata.create_all(bind=engine)

def seed_db():
    db = next(get_db())
    try:
        if not db.query(Sport).first():
            sport = Sport(name="Pool", slug="pool")
            db.add(sport)
            db.commit()
            db.refresh(sport)
            db.add(Discipline(sport_id=sport.id, name="8-Ball", slug="8ball"))
            db.commit()
    finally:
        db.close()

seed_db()


# ─── SOUTH AFRICA LOCATION DATA ───────────────────────────────
SA_PROVINCES = [
    "Eastern Cape", "Free State", "Gauteng", "KwaZulu-Natal",
    "Limpopo", "Mpumalanga", "North West", "Northern Cape", "Western Cape"
]

SA_CITIES = {
    "Eastern Cape": ["Bhisho", "East London", "Gqeberha", "Grahamstown", "Mthatha"],
    "Free State": ["Bloemfontein", "Welkom"],
    "Gauteng": ["Johannesburg", "Pretoria", "Soweto"],
    "KwaZulu-Natal": [
        "Pietermaritzburg", "Durban", "Ladysmith", "Newcastle",
        "Port Shepstone", "Richards Bay", "Howick"
    ],
    "Limpopo": ["Polokwane", "Thohoyandou"],
    "Mpumalanga": ["Mbombela", "Witbank"],
    "North West": ["Mahikeng", "Rustenburg"],
    "Northern Cape": ["Kimberley"],
    "Western Cape": ["Cape Town", "George", "Stellenbosch"]
}

SA_SUBURB_TO_CITY = {
    "Northdale": "Pietermaritzburg",
    "Raisethorpe": "Pietermaritzburg",
    "Scottsville": "Pietermaritzburg",
    "Hayfields": "Pietermaritzburg",
    "Cascades": "Pietermaritzburg",
    "Pelham": "Pietermaritzburg",
    "Prestbury": "Pietermaritzburg",
    "Woodlands": "Pietermaritzburg",
    "Athlone": "Pietermaritzburg",
    "Chase Valley": "Pietermaritzburg",
    "Montrose": "Pietermaritzburg",
    "Bisley": "Pietermaritzburg",
    "Bishopstowe": "Pietermaritzburg",
    "Allandale": "Pietermaritzburg",
    "Rosedale": "Pietermaritzburg",
    "Edendale": "Pietermaritzburg",
    "Impendle": "Pietermaritzburg",
    "Umhlanga": "Durban",
    "Ballito": "Durban",
    "Westville": "Durban",
    "Pinetown": "Durban",
    "Kloof": "Durban",
    "Hillcrest": "Durban",
    "Amanzimtoti": "Durban",
    "Bluff": "Durban",
    "Chatsworth": "Durban",
    "Phoenix": "Durban",
    "Verulam": "Durban",
    "Sandton": "Johannesburg",
    "Randburg": "Johannesburg",
    "Rosebank": "Johannesburg",
    "Fourways": "Johannesburg",
    "Midrand": "Johannesburg",
    "Soweto": "Johannesburg",
    "Alexandra": "Johannesburg",
    "Lenasia": "Johannesburg",
    "Centurion": "Pretoria",
    "Hatfield": "Pretoria",
    "Menlyn": "Pretoria",
    "Sunnyside": "Pretoria",
    "Arcadia": "Pretoria",
    "Brooklyn": "Pretoria",
    "Claremont": "Cape Town",
    "Rondebosch": "Cape Town",
    "Observatory": "Cape Town",
    "Sea Point": "Cape Town",
    "Green Point": "Cape Town",
    "Mitchells Plain": "Cape Town",
    "Khayelitsha": "Cape Town",
    "Bellville": "Cape Town",
    "Durbanville": "Cape Town",
    "Kraaifontein": "Cape Town",
    "Brackenfell": "Cape Town",
    "Goodwood": "Cape Town",
    "Parow": "Cape Town",
    "Table View": "Cape Town",
    "Milnerton": "Cape Town",
}

def canonicalize_city(city_raw: Optional[str], suburb_raw: Optional[str] = None) -> tuple:
    if not city_raw:
        return (None, None)
    city_clean = city_raw.strip().title()
    suburb_clean = (suburb_raw or "").strip().title() or None
    for province, cities in SA_CITIES.items():
        if city_clean in cities:
            return (city_clean, suburb_clean)
    if city_clean in SA_SUBURB_TO_CITY:
        canonical = SA_SUBURB_TO_CITY[city_clean]
        return (canonical, suburb_clean or city_clean)
    return (city_clean, suburb_clean)


@app.get("/locations/provinces")
def list_provinces():
    return {"provinces": SA_PROVINCES}

@app.get("/locations/cities")
def list_cities(province: str):
    province_key = province.strip().title()
    cities = SA_CITIES.get(province_key, [])
    return {"province": province_key, "cities": cities}


# ─── HELPERS ──────────────────────────────────────────────────
def resolve_venue_id(venue_id_str: Optional[str], db: Session) -> Optional[uuid.UUID]:
    if not venue_id_str:
        return None
    try:
        return uuid.UUID(venue_id_str)
    except ValueError:
        venue = db.query(Venue).filter(Venue.name.ilike(venue_id_str)).first()
        if not venue:
            venue = db.query(Venue).filter(Venue.name.ilike(f"%{venue_id_str}%")).first()
        if not venue:
            normalized = venue_id_str.replace("-", " ")
            venue = db.query(Venue).filter(Venue.name.ilike(f"%{normalized}%")).first()
        return venue.id if venue else None

def get_player_stats(player_id: uuid.UUID, db: Session):
    match_ids = db.query(MatchPlayer.match_id).filter(MatchPlayer.player_id == player_id).subquery()
    results = db.query(ConfirmedResult).filter(ConfirmedResult.match_id.in_(match_ids)).all()
    wins = sum(1 for r in results if r.winner_id == player_id)
    draws = sum(1 for r in results if r.winner_id is None)
    losses = len(results) - wins - draws
    total = wins + losses + draws
    win_rate = round((wins / total) * 100, 1) if total > 0 else 0.0
    return {"wins": wins, "losses": losses, "draws": draws, "win_rate": win_rate}

def require_match_participant(match: Match, player: PlayerProfile, db: Session):
    mp = db.query(MatchPlayer).filter(
        MatchPlayer.match_id == match.id,
        MatchPlayer.player_id == player.id
    ).first()
    if not mp:
        raise HTTPException(
            status_code=403,
            detail="You are not a participant in this match"
        )
    return mp

def build_match_response(match: Match, db: Session):
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

    racks = db.query(MatchRack).filter(MatchRack.match_id == match.id).order_by(MatchRack.rack_number).all()
    rack_list = []
    for r in racks:
        winner = db.query(PlayerProfile).filter(PlayerProfile.id == r.winner_player_id).first()
        rack_list.append({
            "rack_number": r.rack_number,
            "winner_player_id": str(r.winner_player_id),
            "winner_name": f"{winner.first_name} {winner.last_name}" if winner else None
        })

    score = {"player_1": 0, "player_2": 0}
    target_wins = match.format_value
    players_ordered = db.query(MatchPlayer).filter(MatchPlayer.match_id == match.id).order_by(MatchPlayer.player_slot).all()
    if len(players_ordered) >= 2:
        p1_id = players_ordered[0].player_id
        p2_id = players_ordered[1].player_id
        for r in racks:
            if r.winner_player_id == p1_id:
                score["player_1"] += 1
            elif r.winner_player_id == p2_id:
                score["player_2"] += 1

    player_1 = next((p for p in players if p["slot"] == 1), None)
    player_2 = next((p for p in players if p["slot"] == 2), None)

    return {
        "id": str(match.id),
        "match_id": str(match.id),
        "status": match.status,
        "context": match.context,
        "format_value": match.format_value,
        "target_wins": target_wins,
        "scheduled_at": match.scheduled_at.isoformat() if match.scheduled_at else None,
        "venue": venue,
        "players": players,
        "player_1": player_1,
        "player_2": player_2,
        "player_1_id": player_1["player_id"] if player_1 else None,
        "player_2_id": player_2["player_id"] if player_2 else None,
        "racks": rack_list,
        "score": score,
        "created_by": str(match.created_by),
        "waiting_on": str(match.waiting_on_player_id) if match.waiting_on_player_id else None,
        "proposal_count": match.proposal_count or 0,
        "started_at": match.started_at.isoformat() if match.started_at else None,
        "paused_at": match.paused_at.isoformat() if match.paused_at else None,
        "result_proposed_by": str(match.result_proposed_by) if match.result_proposed_by else None,
        "result_proposed_at": match.result_proposed_at.isoformat() if match.result_proposed_at else None,
    }

# ─── AUTH UTILS ───────────────────────────────────────────────
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(
    db: Session = Depends(get_db),
    token: str = Query(None, alias="token"),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    auth_token = token
    if authorization and authorization.lower().startswith("bearer "):
        auth_token = authorization[7:].strip()
    if not auth_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(auth_token, SECRET_KEY, algorithms=[ALGORITHM])
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
    suburb: Optional[str] = Field(None, max_length=100)
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

class RecordRackRequest(BaseModel):
    winner_player_id: str

class UpdateRackRequest(BaseModel):
    winner_player_id: str

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

class JoinLeagueRequest(BaseModel):
    team_name: Optional[str] = None

class ApproveJoinRequest(BaseModel):
    player_id: str
    league_id: Optional[str] = None

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
WEAK_PINS = {
    "0000", "1111", "2222", "3333", "4444", "5555", "6666", "7777", "8888", "9999",
    "1234", "4321", "1212", "6969", "1004", "2000", "2024", "2025", "2026",
    "2580", "0852", "1379", "9753", "0007", "0070", "1000", "9999"
}

@app.post("/auth/pin/set")
def set_pin(req: SetPinRequest, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
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
def verify_pin(
    req: VerifyPinRequest,
    db: Session = Depends(get_db),
    token: str = Query(None, alias="token"),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    auth_token = token
    if authorization and authorization.lower().startswith("bearer "):
        auth_token = authorization[7:].strip()
    if not auth_token:
        raise HTTPException(status_code=401, detail="No token provided")
    try:
        payload = jwt.decode(auth_token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.id == uuid.UUID(user_id)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.pin_locked_until and user.pin_locked_until > datetime.utcnow():
        raise HTTPException(status_code=403, detail="PIN locked. Use mobile OTP to unlock.")
    if not user.pin_hash:
        raise HTTPException(status_code=400, detail="No PIN set. Use mobile OTP.")
    if not pwd_context.verify(req.pin, user.pin_hash):
        user.pin_attempts = (user.pin_attempts or 0) + 1
        if user.pin_attempts >= 5:
            user.pin_locked_until = datetime.utcnow() + timedelta(minutes=30)
            db.commit()
            raise HTTPException(status_code=403, detail="Too many failed attempts. PIN locked for 30 minutes. Use mobile OTP.")
        db.commit()
        remaining = 5 - user.pin_attempts
        raise HTTPException(status_code=401, detail=f"Incorrect PIN. {remaining} attempts remaining.")
    user.pin_attempts = 0
    db.commit()
    fresh_token = create_access_token({"sub": str(user.id)})
    return {"token": fresh_token, "valid": True}

@app.post("/auth/pin/reset")
def reset_pin(player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
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
    user = db.query(User).filter(User.mobile_number == mobile_number).first()
    if channel == "whatsapp":
        message = f"🎱 Your RANKD code: 000000\n\nValid for 10 minutes. Don't share this with anyone."
    else:
        message = "Your RANKD code is 000000. Valid for 10 minutes."
    return {
        "sent": True,
        "message": f"OTP sent via {channel.upper()} (mock: 000000)",
        "exists": user is not None,
        "channel": channel,
        "hint": "Enter 000000 to continue"
    }

@app.post("/auth/refresh")
def refresh_token(player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
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
    canonical_city, canonical_suburb = canonicalize_city(req.city, req.suburb)
    profile = PlayerProfile(
        user_id=user.id,
        first_name=req.first_name,
        last_name=req.last_name,
        username=req.username,
        rankd_code=rankd_code,
        city=canonical_city,
        suburb=canonical_suburb,
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
                match.status = "ACTIVE"
                match.started_at = datetime.utcnow()
                match.accepted_at = datetime.utcnow()
                match.waiting_on_player_id = None
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
    stats = get_player_stats(player.id, db)
    return {
        "id": str(player.id),
        "first_name": player.first_name,
        "last_name": player.last_name,
        "username": player.username,
        "rankd_code": player.rankd_code,
        "city": player.city,
        "suburb": player.suburb,
        "province": player.province,
        "country": player.country,
        "street_address": player.street_address,
        "rating": rating.public_rating if rating else 800,
        "public_rating": rating.public_rating if rating else 800,
        "status": rating.status if rating else "UNRANKD",
        "matches_played": rating.matches_played if rating else 0,
        "placement_matches": rating.matches_played if rating else 0,
        "placement_total": 10,
        "unique_opponents": rating.unique_opponents if rating else 0,
        **stats
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
    stats = get_player_stats(p.id, db)
    return {
        "id": str(p.id), "first_name": p.first_name, "last_name": p.last_name,
        "username": p.username, "rankd_code": p.rankd_code,
        "city": p.city, "province": p.province,
        "rating": rating.public_rating if rating else 800,
        "status": rating.status if rating else "UNRANKD",
        "matches_played": rating.matches_played if rating else 0,
        **stats
    }

# ─── LEADERBOARD & CHALLENGE ELIGIBILITY ──────────────────────
@app.get("/leaderboards/{scope}")
def get_leaderboard(scope: str, city: Optional[str] = None, db: Session = Depends(get_db)):
    from sqlalchemy import func
    q = db.query(PlayerProfile, RatingProfile).outerjoin(
        RatingProfile, PlayerProfile.id == RatingProfile.player_id
    )
    if scope == "city" and city:
        canonical = city.strip().title()
        if canonical in SA_SUBURB_TO_CITY:
            canonical = SA_SUBURB_TO_CITY[canonical]
        q = q.filter(PlayerProfile.city == canonical)
    results = q.order_by(func.coalesce(RatingProfile.public_rating, 800).desc()).limit(100).all()
    out = []
    for idx, (profile, rating) in enumerate(results, 1):
        out.append({
            "rank": idx,
            "player_id": str(profile.id),
            "name": f"{profile.first_name} {profile.last_name}",
            "username": profile.username,
            "rating": rating.public_rating if rating else 800,
            "city": profile.city,
            "suburb": profile.suburb,
            "status": rating.status if rating else "UNRANKD",
            "matches": rating.matches_played if rating else 0
        })
    return {"scope": scope, "city": city, "players": out}

@app.get("/challenges/eligibility/{opponent_id}")
def check_challenge_eligibility(
    opponent_id: str,
    player: PlayerProfile = Depends(get_current_player),
    db: Session = Depends(get_db)
):
    from sqlalchemy import func
    opponent = db.query(PlayerProfile).filter(PlayerProfile.id == uuid.UUID(opponent_id)).first()
    if not opponent:
        raise HTTPException(status_code=404, detail="Opponent not found")
    my_rating = db.query(RatingProfile).filter(RatingProfile.player_id == player.id).first()
    opp_rating = db.query(RatingProfile).filter(RatingProfile.player_id == opponent.id).first()
    city = player.city or opponent.city or ""
    lb = db.query(PlayerProfile, RatingProfile).outerjoin(
        RatingProfile, PlayerProfile.id == RatingProfile.player_id
    ).filter(
        PlayerProfile.city.ilike(f"%{city}%")
    ).order_by(func.coalesce(RatingProfile.public_rating, 800).desc()).all()
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
        rank_diff = my_rank - opp_rank
        if opp_rank < my_rank:
            spots_above = my_rank - opp_rank
            if spots_above > 5:
                eligible = False
                reason = f"You can only challenge up to 5 spots above you. They are #{opp_rank}, you are #{my_rank}."
    return {
        "eligible": eligible,
        "reason": reason,
        "my_rank": my_rank,
        "opponent_rank": opp_rank,
        "rank_difference": rank_diff,
        "my_rating": my_rating.public_rating if my_rating else 800,
        "opponent_rating": opp_rating.public_rating if opp_rating else 800
    }

# ─── MATCHES & CHALLENGE NEGOTIATION ──────────────────────────
@app.post("/matches")
def create_match(req: CreateMatchRequest, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    discipline = db.query(Discipline).filter(Discipline.slug == "8ball").first()

    if req.opponent_id and req.context == "LADDER_CHALLENGE":
        from sqlalchemy import func
        opp = db.query(PlayerProfile).filter(PlayerProfile.id == uuid.UUID(req.opponent_id)).first()
        if opp:
            city = player.city or opp.city or ""
            lb = db.query(PlayerProfile, RatingProfile).outerjoin(
                RatingProfile, PlayerProfile.id == RatingProfile.player_id
            ).filter(
                PlayerProfile.city.ilike(f"%{city}%")
            ).order_by(func.coalesce(RatingProfile.public_rating, 800).desc()).all()
            my_rank = None
            opp_rank = None
            for idx, (p, r) in enumerate(lb, 1):
                if str(p.id) == str(player.id):
                    my_rank = idx
                if str(p.id) == str(opp.id):
                    opp_rank = idx
            if my_rank and opp_rank and opp_rank < my_rank:
                spots_above = my_rank - opp_rank
                if spots_above > 5:
                    raise HTTPException(
                        status_code=403,
                        detail=f"You can only challenge up to 5 spots above you. They are #{opp_rank}, you are #{my_rank}."
                    )

    venue_id_parsed = resolve_venue_id(req.venue_id, db)

    match = Match(
        discipline_id=discipline.id if discipline else None,
        context=req.context,
        rating_eligible=req.rating_eligible and req.context != "FRIENDLY",
        format_value=req.format_value,
        venue_id=venue_id_parsed,
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
    if match.waiting_on_player_id and str(match.waiting_on_player_id) != str(player.id):
        raise HTTPException(
            status_code=403,
            detail="Only the player being challenged can accept this match"
        )
    existing = db.query(MatchPlayer).filter(MatchPlayer.match_id == match.id, MatchPlayer.player_id == player.id).first()
    if not existing:
        db.add(MatchPlayer(match_id=match.id, player_id=player.id, player_slot=2))
    match.status = "ACTIVE"
    match.started_at = datetime.utcnow()
    match.accepted_at = datetime.utcnow()
    match.waiting_on_player_id = None
    db.commit()
    return build_match_response(match, db)

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
    match.scheduled_at = req.proposed_datetime
    if req.venue_id:
        resolved = resolve_venue_id(req.venue_id, db)
        if resolved:
            match.venue_id = resolved
    match.status = "COUNTER_PENDING"
    match.proposal_count = (match.proposal_count or 0) + 1
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
def get_match(match_id: str, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    match = db.query(Match).filter(Match.id == uuid.UUID(match_id)).first()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")
    require_match_participant(match, player, db)
    return build_match_response(match, db)

@app.post("/matches/{match_id}/start")
def start_match(match_id: str, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    match = db.query(Match).filter(Match.id == uuid.UUID(match_id)).first()
    if not match or match.status != "ACCEPTED":
        raise HTTPException(status_code=400, detail="Match not in accepted state")
    require_match_participant(match, player, db)
    match.status = "ACTIVE"
    db.commit()
    return build_match_response(match, db)

# ─── LIVE MATCH RACKS ─────────────────────────────────────────
@app.post("/matches/{match_id}/racks")
def record_rack(match_id: str, req: RecordRackRequest, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    match = db.query(Match).filter(Match.id == uuid.UUID(match_id)).first()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")
    require_match_participant(match, player, db)
    if match.status != "ACTIVE":
        raise HTTPException(status_code=400, detail="Match is not active")
    
    winner_id = uuid.UUID(req.winner_player_id)
    participants = db.query(MatchPlayer).filter(MatchPlayer.match_id == match.id).all()
    participant_ids = {p.player_id for p in participants}
    if winner_id not in participant_ids:
        raise HTTPException(status_code=400, detail="Winner must be a match participant")
    
    target_wins = match.format_value
    current_racks = db.query(MatchRack).filter(MatchRack.match_id == match.id).all()
    scores = {}
    for p in participants:
        scores[p.player_id] = sum(1 for r in current_racks if r.winner_player_id == p.player_id)
    if any(s >= target_wins for s in scores.values()):
        raise HTTPException(status_code=400, detail="Match is already complete")
    
    next_rack = len(current_racks) + 1
    existing = db.query(MatchRack).filter(
        MatchRack.match_id == match.id,
        MatchRack.rack_number == next_rack
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Rack already recorded")
    
    rack = MatchRack(
        match_id=match.id,
        rack_number=next_rack,
        winner_player_id=winner_id,
        recorded_by_player_id=player.id
    )
    db.add(rack)
    db.commit()
    return build_match_response(match, db)

@app.patch("/matches/{match_id}/racks/{rack_number}")
def update_rack(match_id: str, rack_number: int, req: UpdateRackRequest, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    match = db.query(Match).filter(Match.id == uuid.UUID(match_id)).first()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")
    require_match_participant(match, player, db)
    if match.status not in ("ACTIVE", "PAUSED", "RESULT_PENDING"):
        raise HTTPException(status_code=400, detail="Cannot modify racks in this match state")
    
    rack = db.query(MatchRack).filter(
        MatchRack.match_id == match.id,
        MatchRack.rack_number == rack_number
    ).first()
    if not rack:
        raise HTTPException(status_code=404, detail="Rack not found")
    
    winner_id = uuid.UUID(req.winner_player_id)
    participants = db.query(MatchPlayer).filter(MatchPlayer.match_id == match.id).all()
    participant_ids = {p.player_id for p in participants}
    if winner_id not in participant_ids:
        raise HTTPException(status_code=400, detail="Winner must be a participant")
    
    rack.winner_player_id = winner_id
    rack.recorded_by_player_id = player.id
    rack.updated_at = datetime.utcnow()
    db.commit()
    return build_match_response(match, db)

@app.post("/matches/{match_id}/pause")
def pause_match(match_id: str, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    match = db.query(Match).filter(Match.id == uuid.UUID(match_id)).first()
    if not match or match.status != "ACTIVE":
        raise HTTPException(status_code=400, detail="Match is not active")
    require_match_participant(match, player, db)
    match.status = "PAUSED"
    match.paused_at = datetime.utcnow()
    db.commit()
    return build_match_response(match, db)

@app.post("/matches/{match_id}/resume")
def resume_match(match_id: str, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    match = db.query(Match).filter(Match.id == uuid.UUID(match_id)).first()
    if not match or match.status != "PAUSED":
        raise HTTPException(status_code=400, detail="Match is not paused")
    require_match_participant(match, player, db)
    match.status = "ACTIVE"
    db.commit()
    return build_match_response(match, db)

@app.post("/matches/{match_id}/forfeit")
def forfeit_match(
    match_id: str,
    player: PlayerProfile = Depends(get_current_player),
    db: Session = Depends(get_db)
):
    try:
        match_uuid = uuid.UUID(match_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid match ID")

    match = db.query(Match).filter(Match.id == match_uuid).first()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    require_match_participant(match, player, db)

    if match.status not in ("ACTIVE", "PAUSED"):
        raise HTTPException(
            status_code=400,
            detail=f"Match cannot be forfeited while status is {match.status}"
        )

    players = (
        db.query(MatchPlayer)
        .filter(MatchPlayer.match_id == match.id)
        .order_by(MatchPlayer.player_slot)
        .all()
    )

    if len(players) != 2:
        raise HTTPException(
            status_code=400,
            detail="Match must have exactly two players"
        )

    p1 = players[0]
    p2 = players[1]

    if p1.player_id == player.id:
        forfeiter = p1
        winner = p2
    elif p2.player_id == player.id:
        forfeiter = p2
        winner = p1
    else:
        raise HTTPException(
            status_code=403,
            detail="You are not a participant in this match"
        )

    racks = (
        db.query(MatchRack)
        .filter(MatchRack.match_id == match.id)
        .all()
    )

    score_p1 = sum(
        1 for r in racks
        if r.winner_player_id == p1.player_id
    )

    score_p2 = sum(
        1 for r in racks
        if r.winner_player_id == p2.player_id
    )

    target = match.format_value

    if winner.player_id == p1.player_id:
        score_p1 = target
        score_p2 = min(score_p2, target - 1)
    else:
        score_p2 = target
        score_p1 = min(score_p1, target - 1)

    existing_result = (
        db.query(ConfirmedResult)
        .filter(ConfirmedResult.match_id == match.id)
        .first()
    )

    if existing_result:
        raise HTTPException(
            status_code=409,
            detail="Match already has a confirmed result"
        )

    p1.is_winner = (p1.player_id == winner.player_id)
    p2.is_winner = (p2.player_id == winner.player_id)

    match.status = "CONFIRMED"
    match.result_confirmed_by = player.id
    match.confirmed_at = datetime.utcnow()

    confirmed = ConfirmedResult(
        match_id=match.id,
        player_a_score=score_p1,
        player_b_score=score_p2,
        winner_id=winner.player_id,
        confirmed_at=datetime.utcnow(),
        confirmed_by_algorithm=True
    )

    db.add(confirmed)
    db.commit()

    rating_result = None

    if (
        match.rating_eligible
        and match.context != "FRIENDLY"
    ):
        try:
            rating_result = calculate_ratings(
                match,
                score_p1,
                score_p2,
                db
            )
        except Exception as e:
            db.rollback()
            print(
                f"CRITICAL RATING FAILURE (forfeit) "
                f"match={match.id} "
                f"error={repr(e)}"
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Forfeit saved but rating "
                    f"calculation failed: {str(e)}"
                )
            )

    try:
        update_rivalry(
            match,
            score_p1,
            score_p2,
            db
        )
    except Exception as e:
        print(f"CRITICAL RIVALRY FAILURE (forfeit) match={match.id} error={repr(e)}")

    winner_profile = (
        db.query(PlayerProfile)
        .filter(PlayerProfile.id == winner.player_id)
        .first()
    )

    forfeiter_profile = (
        db.query(PlayerProfile)
        .filter(PlayerProfile.id == forfeiter.player_id)
        .first()
    )

    return {
        "success": True,
        "status": "CONFIRMED",
        "match_id": str(match.id),
        "forfeited_by": {
            "player_id": str(forfeiter.player_id),
            "name": (
                f"{forfeiter_profile.first_name} {forfeiter_profile.last_name}"
                if forfeiter_profile else None
            )
        },
        "winner": {
            "player_id": str(winner.player_id),
            "name": (
                f"{winner_profile.first_name} {winner_profile.last_name}"
                if winner_profile else None
            )
        },
        "score": {
            "player_1": score_p1,
            "player_2": score_p2
        },
        "rating": rating_result
    }

# ─── RESULT PROPOSAL / ACCEPT / DENY ───────────────────────────
@app.post("/matches/{match_id}/result/propose")
def propose_result(match_id: str, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    match = db.query(Match).filter(Match.id == uuid.UUID(match_id)).first()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")
    require_match_participant(match, player, db)
    if match.status != "ACTIVE":
        raise HTTPException(status_code=400, detail="Match must be active to propose result")
    
    racks = db.query(MatchRack).filter(MatchRack.match_id == match.id).all()
    players = db.query(MatchPlayer).filter(MatchPlayer.match_id == match.id).order_by(MatchPlayer.player_slot).all()
    if len(players) != 2:
        raise HTTPException(status_code=400, detail="Invalid match player count")
    
    p1_id = players[0].player_id
    p2_id = players[1].player_id
    score_p1 = sum(1 for r in racks if r.winner_player_id == p1_id)
    score_p2 = sum(1 for r in racks if r.winner_player_id == p2_id)
    target = match.format_value
    
    if score_p1 < target and score_p2 < target:
        raise HTTPException(status_code=400, detail=f"Match is not complete. Need {target} wins to finish.")
    
    match.status = "RESULT_PENDING"
    match.result_proposed_by = player.id
    match.result_proposed_at = datetime.utcnow()
    db.commit()
    return {
        "status": "RESULT_PENDING",
        "score": {"player_1": score_p1, "player_2": score_p2}
    }

@app.post("/matches/{match_id}/result/accept")
def accept_result(match_id: str, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    match = db.query(Match).filter(Match.id == uuid.UUID(match_id)).first()
    if not match or match.status != "RESULT_PENDING":
        raise HTTPException(status_code=400, detail="No result pending")
    require_match_participant(match, player, db)
    if match.result_proposed_by == player.id:
        raise HTTPException(status_code=400, detail="You cannot accept your own result")

    racks = db.query(MatchRack).filter(MatchRack.match_id == match.id).all()
    players = db.query(MatchPlayer).filter(MatchPlayer.match_id == match.id).order_by(MatchPlayer.player_slot).all()
    if len(players) != 2:
        raise HTTPException(status_code=400, detail="Invalid match")
    p1_id = players[0].player_id
    p2_id = players[1].player_id
    score_p1 = sum(1 for r in racks if r.winner_player_id == p1_id)
    score_p2 = sum(1 for r in racks if r.winner_player_id == p2_id)

    winner_id = match.proposed_winner_id
    if not winner_id:
        if score_p1 > score_p2:
            winner_id = p1_id
        elif score_p2 > score_p1:
            winner_id = p2_id
        else:
            winner_id = None

    rating_result = None

    existing_confirmed = (
        db.query(ConfirmedResult)
        .filter(ConfirmedResult.match_id == match.id)
        .first()
    )

    if not existing_confirmed:
        confirmed = ConfirmedResult(
            match_id=match.id,
            player_a_score=score_p1,
            player_b_score=score_p2,
            winner_id=winner_id,
            confirmed_by=player.id,
            confirmed_at=datetime.utcnow()
        )
        db.add(confirmed)

    match.status = "CONFIRMED"
    match.confirmed_at = datetime.utcnow()
    match.confirmed_by = player.id
    match.winner_id = winner_id

    for mp in players:
        mp.is_winner = (mp.player_id == winner_id)

    db.commit()

    if (
        match.rating_eligible
        and match.context != "FRIENDLY"
    ):
        try:
            rating_result = calculate_ratings(
                match, score_p1, score_p2, db
            )
        except Exception as e:
            db.rollback()
            print(
                f"CRITICAL RATING FAILURE "
                f"match={match.id} "
                f"error={repr(e)}"
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Match result saved but rating "
                    f"calculation failed: {str(e)}"
                )
            )

    try:
        update_rivalry(match, score_p1, score_p2, db)
    except Exception as e:
        print(f"CRITICAL RIVALRY FAILURE match={match.id} error={repr(e)}")

    return {
        "status": "CONFIRMED",
        "match_id": match_id,
        "rating": rating_result
    }
@app.post("/matches/{match_id}/result/deny")
def deny_result(match_id: str, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    match = db.query(Match).filter(Match.id == uuid.UUID(match_id)).first()
    if not match or match.status != "RESULT_PENDING":
        raise HTTPException(status_code=400, detail="No result pending")
    require_match_participant(match, player, db)
    if match.result_proposed_by == player.id:
        raise HTTPException(status_code=400, detail="You cannot deny your own result")
    
    match.result_denied_by = player.id
    match.result_denied_at = datetime.utcnow()
    match.result_proposed_by = None
    match.result_proposed_at = None
    match.status = "ACTIVE"
    db.commit()
    return {"status": "ACTIVE", "message": "Result denied. Match remains in progress."}

@app.get("/matches/my/active")
def get_my_active_matches(
    player: PlayerProfile = Depends(get_current_player),
    db: Session = Depends(get_db)
):
    mps = (
        db.query(MatchPlayer)
        .filter(MatchPlayer.player_id == player.id)
        .all()
    )

    match_ids = [mp.match_id for mp in mps]

    if not match_ids:
        return {"matches": []}

    matches = (
        db.query(Match)
        .filter(
            Match.id.in_(match_ids),
            Match.status.in_([
                "ACTIVE",
                "PAUSED",
                "RESULT_PENDING"
            ])
        )
        .order_by(Match.updated_at.desc())
        .all()
    )

    out = []

    for match in matches:

        # ------------------------------------
        # SAFETY CHECK:
        # A confirmed match can NEVER be active.
        # ------------------------------------
        confirmed = (
            db.query(ConfirmedResult)
            .filter(ConfirmedResult.match_id == match.id)
            .first()
        )

        if confirmed:
            # Repair stale match state
            if match.status != "CONFIRMED":
                match.status = "CONFIRMED"

                if not match.confirmed_at:
                    match.confirmed_at = (
                        confirmed.confirmed_at
                        or datetime.utcnow()
                    )

                db.commit()

            # Do not send this match to Home
            continue

        all_mps = (
            db.query(MatchPlayer)
            .filter(MatchPlayer.match_id == match.id)
            .all()
        )

        opponent_mp = next(
            (
                mp for mp in all_mps
                if mp.player_id != player.id
            ),
            None
        )

        opponent = None

        if opponent_mp:
            opp_profile = (
                db.query(PlayerProfile)
                .filter(
                    PlayerProfile.id ==
                    opponent_mp.player_id
                )
                .first()
            )

            if opp_profile:
                opponent = {
                    "id": str(opp_profile.id),
                    "name": (
                        f"{opp_profile.first_name} "
                        f"{opp_profile.last_name}"
                    ),
                    "username": opp_profile.username
                }

        racks = (
            db.query(MatchRack)
            .filter(MatchRack.match_id == match.id)
            .all()
        )

        my_score = sum(
            1
            for r in racks
            if r.winner_player_id == player.id
        )

        opp_score = sum(
            1
            for r in racks
            if opponent_mp
            and r.winner_player_id == opponent_mp.player_id
        )

        venue_name = None

        if match.venue_id:
            venue = (
                db.query(Venue)
                .filter(Venue.id == match.venue_id)
                .first()
            )

            venue_name = venue.name if venue else None

        out.append({
            "match_id": str(match.id),
            "status": match.status,
            "opponent": opponent,
            "score": {
                "me": my_score,
                "opponent": opp_score
            },
            "format_value": match.format_value,
            "venue": venue_name
        })

    return {"matches": out}


@app.post("/matches/{match_id}/result")
def submit_result(match_id: str, req: SubmitResultRequest, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    match = db.query(Match).filter(Match.id == uuid.UUID(match_id)).first()
    if not match or match.status not in ("ACTIVE", "AWAITING_RESULT", "RESULT_PARTIAL"):
        raise HTTPException(status_code=400, detail="Match not accepting results")
    mp = db.query(MatchPlayer).filter(MatchPlayer.match_id == match.id, MatchPlayer.player_id == player.id).first()
    if not mp:
        raise HTTPException(status_code=403, detail="You are not in this match")

    if mp.player_slot == 2:
        norm_a = req.player_b_score
        norm_b = req.player_a_score
    else:
        norm_a = req.player_a_score
        norm_b = req.player_b_score

    existing_sub = db.query(ResultSubmission).filter(
        ResultSubmission.match_id == match.id,
        ResultSubmission.player_id == player.id
    ).first()
    if existing_sub:
        existing_sub.player_a_score = norm_a
        existing_sub.player_b_score = norm_b
        existing_sub.revision += 1
    else:
        db.add(ResultSubmission(
            match_id=match.id, player_id=player.id,
            player_a_score=norm_a, player_b_score=norm_b
        ))

    all_subs = db.query(ResultSubmission).filter(ResultSubmission.match_id == match.id).all()
    if len(all_subs) == 1:
        match.status = "RESULT_PARTIAL"
    elif len(all_subs) >= 2:
        if all_subs[0].player_a_score == all_subs[1].player_a_score and all_subs[0].player_b_score == all_subs[1].player_b_score:
            match.status = "CONFIRMED"
            match.confirmed_at = datetime.utcnow()

            players_ordered = db.query(MatchPlayer).filter(MatchPlayer.match_id == match.id).order_by(MatchPlayer.player_slot).all()
            winner_id = None
            if norm_a > norm_b:
                winner_id = players_ordered[0].player_id
            elif norm_b > norm_a:
                winner_id = players_ordered[1].player_id

            db.add(ConfirmedResult(
                match_id=match.id,
                player_a_score=norm_a,
                player_b_score=norm_b,
                winner_id=winner_id,
                confirmed_by_algorithm=True
            ))
            if match.rating_eligible:
                calculate_ratings(match, norm_a, norm_b, db)
            update_rivalry(match, norm_a, norm_b, db)
        else:
            match.status = "RESULT_MISMATCH"
    db.commit()
    return {"match_id": match_id, "status": match.status}

def get_unique_opponent_ids(player_id: uuid.UUID, db: Session) -> set:
    """
    Return the actual IDs of every unique opponent this player
    has completed a confirmed match against.
    """
    my_matches = (
        db.query(MatchPlayer.match_id)
        .filter(MatchPlayer.player_id == player_id)
        .subquery()
    )

    opponent_rows = (
        db.query(MatchPlayer.player_id)
        .join(
            ConfirmedResult,
            ConfirmedResult.match_id == MatchPlayer.match_id
        )
        .filter(
            MatchPlayer.match_id.in_(my_matches),
            MatchPlayer.player_id != player_id
        )
        .distinct()
        .all()
    )

    return {str(row[0]) for row in opponent_rows}


def calculate_ratings(match: Match, score_a: int, score_b: int, db: Session):
    # HARD SAFETY RULE: Friendly matches NEVER affect RANKD rating.
    if match.context == "FRIENDLY":
        return None
    if not match.rating_eligible:
        return None

    players = db.query(MatchPlayer).filter(MatchPlayer.match_id == match.id).order_by(MatchPlayer.player_slot).all()
    if len(players) != 2:
        raise RuntimeError(
            f"Rating failed: expected 2 players, got {len(players)}"
        )

    p1_id, p2_id = str(players[0].player_id), str(players[1].player_id)
    rp1 = db.query(RatingProfile).filter(RatingProfile.player_id == players[0].player_id).first()
    rp2 = db.query(RatingProfile).filter(RatingProfile.player_id == players[1].player_id).first()
    if not rp1 or not rp2:
        raise RuntimeError(
            f"Rating failed: missing RatingProfile "
            f"rp1={bool(rp1)} rp2={bool(rp2)}"
        )

    print(
        f"RATING START "
        f"match={match.id} "
        f"p1_old={rp1.public_rating} "
        f"p2_old={rp2.public_rating}"
    )

    s1 = engine_service.register_player(p1_id, mu=float(rp1.mu), phi=float(rp1.phi))
    s2 = engine_service.register_player(p2_id, mu=float(rp2.mu), phi=float(rp2.phi))
    s1.volatility = float(rp1.volatility)
    s2.volatility = float(rp2.volatility)

    s1.unique_opponents = get_unique_opponent_ids(
        players[0].player_id,
        db
    )
    s2.unique_opponents = get_unique_opponent_ids(
        players[1].player_id,
        db
    )

    s1.matches_played = rp1.matches_played or 0
    s2.matches_played = rp2.matches_played or 0
    s1.status = rp1.status or "UNRANKD"
    s2.status = rp2.status or "UNRANKD"

    result = engine_service.calculate_match(
        player_a_id=p1_id,
        player_b_id=p2_id,
        score_a=score_a,
        score_b=score_b,
        match_id=str(match.id)
    )

    rp1.mu = s1.mu
    rp1.phi = s1.phi
    rp1.volatility = s1.volatility
    rp1.matches_played = s1.matches_played
    rp1.status = s1.status
    rp1.public_rating = s1.public_rating_display
    rp1.last_match_at = datetime.utcnow()
    rp1.unique_opponents = len(s1.unique_opponents)

    rp2.mu = s2.mu
    rp2.phi = s2.phi
    rp2.volatility = s2.volatility
    rp2.matches_played = s2.matches_played
    rp2.status = s2.status
    rp2.public_rating = s2.public_rating_display
    rp2.last_match_at = datetime.utcnow()
    rp2.unique_opponents = len(s2.unique_opponents)

    db.commit()

    print(
        f"RATING SAVED "
        f"match={match.id} "
        f"p1_new={rp1.public_rating} "
        f"p2_new={rp2.public_rating}"
    )

    # Save RatingEvent rows separately so an audit failure
    # does not roll back the actual player ratings.
    try:
        events = getattr(
            engine_service,
            'rating_events',
            []
        )

        recent_events = (
            events[-2:]
            if len(events) >= 2
            else events
        )

        for ev in recent_events:
            db.add(
                RatingEvent(
                    match_id=uuid.UUID(ev.match_id),
                    player_id=uuid.UUID(ev.player_id),

                    rating_profile_id=(
                        rp1.id
                        if ev.player_id == p1_id
                        else rp2.id
                    ),

                    algorithm_version=ev.algorithm_version,

                    old_rating=ev.old_rating,
                    new_rating=ev.new_rating,
                    delta=ev.delta,

                    old_mu=ev.old_mu,
                    new_mu=ev.new_mu,

                    old_phi=ev.old_phi,
                    new_phi=ev.new_phi,

                    old_volatility=ev.old_volatility,
                    new_volatility=ev.new_volatility,

                    information_weight=ev.information_weight,
                    integrity_weight=ev.integrity_weight,
                    familiarity_factor=ev.familiarity_factor,

                    opponent_id=uuid.UUID(ev.opponent_id),

                    score=ev.score,
                    result=ev.result,
                    explanation_code=ev.explanation_code
                )
            )

        db.commit()

    except Exception as e:
        db.rollback()

        print(
            f"RATING EVENT SAVE FAILED "
            f"match={match.id} "
            f"error={repr(e)}"
        )

    return result
def update_rivalry(match: Match, score_a: int, score_b: int, db: Session):
    mps = db.query(MatchPlayer).filter(MatchPlayer.match_id == match.id).order_by(MatchPlayer.player_slot).all()
    if len(mps) != 2:
        return

    slot1_id, slot2_id = mps[0].player_id, mps[1].player_id

    ids_sorted = sorted([str(slot1_id), str(slot2_id)])
    pa, pb = uuid.UUID(ids_sorted[0]), uuid.UUID(ids_sorted[1])

    rivalry = db.query(Rivalry).filter(
        Rivalry.player_a_id == pa, Rivalry.player_b_id == pb
    ).first()
    if not rivalry:
        rivalry = Rivalry(player_a_id=pa, player_b_id=pb, first_match_at=datetime.utcnow())
        db.add(rivalry)

    rivalry.total_matches += 1
    rivalry.last_match_at = datetime.utcnow()

    if str(slot1_id) == ids_sorted[0]:
        rivalry.total_frames_a += score_a
        rivalry.total_frames_b += score_b
        if score_a > score_b:
            rivalry.player_a_wins += 1
            rivalry.current_streak_a = (rivalry.current_streak_a + 1) if rivalry.current_streak_a > 0 else 1
            rivalry.current_streak_b = 0
            rivalry.longest_streak_a = max(rivalry.longest_streak_a, rivalry.current_streak_a)
        elif score_b > score_a:
            rivalry.player_b_wins += 1
            rivalry.current_streak_b = (rivalry.current_streak_b + 1) if rivalry.current_streak_b > 0 else 1
            rivalry.current_streak_a = 0
            rivalry.longest_streak_b = max(rivalry.longest_streak_b, rivalry.current_streak_b)
        else:
            rivalry.draws += 1
    else:
        rivalry.total_frames_a += score_b
        rivalry.total_frames_b += score_a
        if score_b > score_a:
            rivalry.player_a_wins += 1
            rivalry.current_streak_a = (rivalry.current_streak_a + 1) if rivalry.current_streak_a > 0 else 1
            rivalry.current_streak_b = 0
            rivalry.longest_streak_a = max(rivalry.longest_streak_a, rivalry.current_streak_a)
        elif score_a > score_b:
            rivalry.player_b_wins += 1
            rivalry.current_streak_b = (rivalry.current_streak_b + 1) if rivalry.current_streak_b > 0 else 1
            rivalry.current_streak_a = 0
            rivalry.longest_streak_b = max(rivalry.longest_streak_b, rivalry.current_streak_b)
        else:
            rivalry.draws += 1

    db.commit()

# ─── NOTIFICATIONS ────────────────────────────────────────────
@app.get("/notifications")
def get_notifications(player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    incoming = db.query(Match).filter(
        Match.waiting_on_player_id == player.id,
        Match.status.in_(["INVITE_PENDING", "COUNTER_PENDING"])
    ).order_by(Match.created_at.desc()).all()
    
    active = db.query(Match).join(MatchPlayer).filter(
        MatchPlayer.player_id == player.id,
        Match.status == "ACTIVE"
    ).order_by(Match.updated_at.desc()).all()
    
    paused = db.query(Match).join(MatchPlayer).filter(
        MatchPlayer.player_id == player.id,
        Match.status == "PAUSED"
    ).order_by(Match.updated_at.desc()).all()
    
    result_pending = db.query(Match).join(MatchPlayer).filter(
        MatchPlayer.player_id == player.id,
        Match.status == "RESULT_PENDING",
        Match.result_proposed_by != player.id
    ).order_by(Match.result_proposed_at.desc()).all()
    
    def match_to_notif(m, ntype):
        creator = db.query(PlayerProfile).filter(PlayerProfile.id == m.created_by).first()
        venue = db.query(Venue).filter(Venue.id == m.venue_id).first() if m.venue_id else None
        all_mps = db.query(MatchPlayer).filter(MatchPlayer.match_id == m.id).all()
        opponent_mp = next((mp for mp in all_mps if mp.player_id != player.id), None)
        opponent = None
        if opponent_mp:
            opp_profile = db.query(PlayerProfile).filter(PlayerProfile.id == opponent_mp.player_id).first()
            opponent = opp_profile
        
        racks = db.query(MatchRack).filter(MatchRack.match_id == m.id).all()
        my_score = sum(1 for r in racks if r.winner_player_id == player.id)
        opp_score = 0
        if opponent_mp:
            opp_score = sum(1 for r in racks if r.winner_player_id == opponent_mp.player_id)
        
        obj = {
            "match_id": str(m.id),
            "type": ntype,
            "status": m.status,
            "from": {
                "name": f"{creator.first_name} {creator.last_name}" if creator else None,
                "username": creator.username if creator else None
            } if creator else None,
            "opponent": {
                "name": f"{opponent.first_name} {opponent.last_name}" if opponent else None,
                "username": opponent.username if opponent else None
            } if opponent else None,
            "venue": venue.name if venue else None,
            "format_value": m.format_value,
            "score": {"me": my_score, "opponent": opp_score},
            "proposal_count": m.proposal_count or 0,
            "result_proposed_by": str(m.result_proposed_by) if m.result_proposed_by else None
        }
        if ntype in ("challenge", "counter"):
            obj["proposed_datetime"] = m.scheduled_at.isoformat() if m.scheduled_at else None
        return obj
    
    incoming_out = [match_to_notif(m, "challenge" if m.status == "INVITE_PENDING" else "counter") for m in incoming]
    active_out = [match_to_notif(m, "active") for m in active]
    paused_out = [match_to_notif(m, "paused") for m in paused]
    result_out = [match_to_notif(m, "result_pending") for m in result_pending]
    
    total_pending = len(incoming) + len(result_pending)
    
    return {
        "incoming_challenges": incoming_out,
        "active_matches": active_out,
        "paused_matches": paused_out,
        "results_waiting_for_me": result_out,
        "pending_count": total_pending
    }

# ─── GUEST INVITES ────────────────────────────────────────────
@app.post("/invites/guest")
def create_guest_invite(req: GuestInviteRequest, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    venue_id_parsed = resolve_venue_id(req.venue_id, db)
    match = Match(
        discipline_id=db.query(Discipline).filter(Discipline.slug == "8ball").first().id,
        context=req.context,
        rating_eligible=req.context != "FRIENDLY",
        format_value=req.format_value,
        venue_id=venue_id_parsed,
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
    inviter_rating = db.query(RatingProfile).filter(RatingProfile.player_id == inviter.id).first() if inviter else None
    return {
        "valid": True,
        "match_id": str(match.id),
        "inviter": {
            "name": inviter.first_name,
            "username": inviter.username,
            "rating": inviter_rating.public_rating if inviter_rating else 800
        },
        "context": match.context,
        "format_value": match.format_value
    }

# ─── LEAGUES ──────────────────────────────────────────────────
@app.post("/leagues")
def create_league(req: CreateLeagueRequest, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == player.user_id).first()
    if not user.is_venue_owner:
        user.is_venue_owner = True
        db.commit()

    venue_id_parsed = resolve_venue_id(req.venue_id, db)
    sport = db.query(Sport).filter(Sport.slug == "pool").first()

    league = League(
        name=req.name, city=req.city, province=req.province,
        venue_id=venue_id_parsed,
        sport_id=uuid.UUID(req.sport_id) if req.sport_id else (sport.id if sport else None),
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
        PlayerLeague.season == "2026"
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Player already in league")
    db.add(PlayerLeague(
        player_id=target.id, league_id=league.id,
        team_id=team_id, division=req.division,
        season="2026"
    ))
    db.commit()
    return {"success": True, "player_id": str(target.id), "league_id": league_id}

@app.post("/leagues/{league_id}/join")
def join_league(league_id: str, req: JoinLeagueRequest = None, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    league = db.query(League).filter(League.id == uuid.UUID(league_id)).first()
    if not league:
        raise HTTPException(status_code=404, detail="League not found")
    existing = db.query(PlayerLeague).filter(
        PlayerLeague.player_id == player.id,
        PlayerLeague.league_id == league.id
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Already requested or joined this league")
    team_id = None
    if req and req.team_name:
        team = db.query(Team).filter(Team.league_id == league.id, Team.name == req.team_name).first()
        if team:
            team_id = team.id
    db.add(PlayerLeague(
        player_id=player.id, league_id=league.id,
        team_id=team_id, season="2026", is_verified=False
    ))
    db.commit()
    return {"success": True, "message": "Join request sent. Awaiting approval."}

@app.get("/leagues/my")
def my_leagues(player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    memberships = db.query(PlayerLeague).filter(PlayerLeague.player_id == player.id).all()
    member_of = []
    for m in memberships:
        league = db.query(League).filter(League.id == m.league_id).first()
        if league:
            team = db.query(Team).filter(Team.id == m.team_id).first() if m.team_id else None
            member_of.append({
                "id": str(league.id),
                "name": league.name,
                "is_verified": m.is_verified,
                "team": team.name if team else None,
                "division": m.division
            })
    created = db.query(League).filter(League.created_by == player.id).all()
    admin_of = []
    for league in created:
        pending = db.query(PlayerLeague).filter(
            PlayerLeague.league_id == league.id,
            PlayerLeague.is_verified == False
        ).all()
        admin_of.append({
            "id": str(league.id),
            "name": league.name,
            "pending_requests": len(pending)
        })
    return {"member_of": member_of, "admin_of": admin_of}

@app.post("/leagues/approve")
def approve_league_join(req: ApproveJoinRequest, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    if req.league_id:
        membership = db.query(PlayerLeague).filter(
            PlayerLeague.player_id == uuid.UUID(req.player_id),
            PlayerLeague.league_id == uuid.UUID(req.league_id)
        ).first()
    else:
        admin_leagues = db.query(League).filter(League.created_by == player.id).all()
        admin_ids = [l.id for l in admin_leagues]
        membership = db.query(PlayerLeague).filter(
            PlayerLeague.player_id == uuid.UUID(req.player_id),
            PlayerLeague.league_id.in_(admin_ids),
            PlayerLeague.is_verified == False
        ).first()
    if not membership:
        raise HTTPException(status_code=404, detail="Join request not found")
    league = db.query(League).filter(League.id == membership.league_id).first()
    if not league or league.created_by != player.id:
        raise HTTPException(status_code=403, detail="Only league creator can approve")
    membership.is_verified = True
    db.commit()
    return {"success": True}

@app.post("/leagues/reject")
def reject_league_join(req: ApproveJoinRequest, player: PlayerProfile = Depends(get_current_player), db: Session = Depends(get_db)):
    if req.league_id:
        membership = db.query(PlayerLeague).filter(
            PlayerLeague.player_id == uuid.UUID(req.player_id),
            PlayerLeague.league_id == uuid.UUID(req.league_id)
        ).first()
    else:
        admin_leagues = db.query(League).filter(League.created_by == player.id).all()
        admin_ids = [l.id for l in admin_leagues]
        membership = db.query(PlayerLeague).filter(
            PlayerLeague.player_id == uuid.UUID(req.player_id),
            PlayerLeague.league_id.in_(admin_ids),
            PlayerLeague.is_verified == False
        ).first()
    if not membership:
        raise HTTPException(status_code=404, detail="Join request not found")
    league = db.query(League).filter(League.id == membership.league_id).first()
    if not league or league.created_by != player.id:
        raise HTTPException(status_code=403, detail="Only league creator can reject")
    db.delete(membership)
    db.commit()
    return {"success": True}

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


# ─── ADMIN: ONE-TIME DATA MIGRATION ───────────────────────────
@app.post("/admin/migrate-suburbs")
def migrate_suburbs(admin_token: str = Query(...), db: Session = Depends(get_db)):
    if admin_token != os.getenv("ADMIN_SECRET", "rankd-admin-2026"):
        raise HTTPException(status_code=403, detail="Invalid admin token")

    from sqlalchemy import text
    try:
        db.execute(text("ALTER TABLE player_profiles ADD COLUMN IF NOT EXISTS suburb VARCHAR(100)"))
        db.execute(text("ALTER TABLE venues ADD COLUMN IF NOT EXISTS suburb VARCHAR(100)"))
        db.commit()
    except Exception as e:
        db.rollback()

    updated = 0
    profiles = db.query(PlayerProfile).all()
    for p in profiles:
        if not p.city:
            continue
        canonical, suburb = canonicalize_city(p.city, p.suburb)
        if canonical != p.city or suburb != p.suburb:
            p.city = canonical
            p.suburb = suburb
            updated += 1

    db.commit()
    return {
        "migrated": updated,
        "message": f"Updated {updated} profiles. Suburbs moved to suburb column, cities canonicalized."
    }

# ─── RUN ──────────────────────────────────────────────────────

@app.get("/matches/my/history")
def get_my_match_history(
    limit: int = 10,
    player: PlayerProfile = Depends(get_current_player),
    db: Session = Depends(get_db)
):
    player_matches = (
        db.query(MatchPlayer)
        .filter(MatchPlayer.player_id == player.id)
        .all()
    )

    match_ids = [mp.match_id for mp in player_matches]

    if not match_ids:
        return {"matches": []}

    matches = (
        db.query(Match)
        .filter(
            Match.id.in_(match_ids),
            Match.status == "CONFIRMED"
        )
        .order_by(Match.confirmed_at.desc())
        .limit(min(limit, 50))
        .all()
    )

    output = []

    for match in matches:
        players = (
            db.query(MatchPlayer)
            .filter(MatchPlayer.match_id == match.id)
            .order_by(MatchPlayer.player_slot)
            .all()
        )

        if len(players) != 2:
            continue

        p1 = players[0]
        p2 = players[1]

        opponent_mp = (
            p2 if p1.player_id == player.id else p1
        )

        opponent = (
            db.query(PlayerProfile)
            .filter(PlayerProfile.id == opponent_mp.player_id)
            .first()
        )

        confirmed = (
            db.query(ConfirmedResult)
            .filter(ConfirmedResult.match_id == match.id)
            .first()
        )

        if not confirmed:
            continue

        is_p1 = p1.player_id == player.id

        my_score = (
            confirmed.player_a_score
            if is_p1
            else confirmed.player_b_score
        )

        opponent_score = (
            confirmed.player_b_score
            if is_p1
            else confirmed.player_a_score
        )

        if confirmed.winner_id == player.id:
            result = "WIN"
        elif confirmed.winner_id is None:
            result = "DRAW"
        else:
            result = "LOSS"

        rating_event = (
            db.query(RatingEvent)
            .filter(
                RatingEvent.match_id == match.id,
                RatingEvent.player_id == player.id
            )
            .first()
        )

        rating_change = (
            rating_event.delta
            if rating_event else 0
        )

        output.append({
            "match_id": str(match.id),

            "opponent": {
                "id": str(opponent.id) if opponent else None,
                "name": (
                    f"{opponent.first_name} {opponent.last_name}"
                    if opponent else "Opponent"
                ),
                "username": (
                    opponent.username if opponent else None
                )
            },

            "result": result,

            "score": {
                "me": my_score,
                "opponent": opponent_score
            },

            "context": match.context,

            "race_to": match.format_value,

            "rated": bool(
                match.rating_eligible
                and match.context != "FRIENDLY"
            ),

            "rating_change": (
                rating_change
                if match.context != "FRIENDLY"
                else 0
            ),

            "played_at": (
                match.confirmed_at.isoformat()
                if match.confirmed_at else None
            )
        })

    return {"matches": output}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
