"""RANKD Rating Engine v1.0 — Glicko-2 inspired with anti-farming."""
import math
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime
from collections import defaultdict

@dataclass
class AlgorithmConfig:
    version: str = "RANKD_V1"
    initial_mu: float = 1500.0
    initial_phi: float = 350.0
    initial_sigma: float = 350.0
    tau: float = 0.5
    epsilon: float = 0.000001
    placement_matches: int = 10
    established_phi_threshold: float = 120.0
    established_min_matches: int = 20
    established_min_opponents: int = 5
    match_info_cap: float = 1.5
    repeat_decay_base: float = 0.7
    repeat_recovery_time: int = 50
    repeat_recovery_rate: float = 0.15
    opponent_familiarity_floor: float = 0.1
    integrity_repeat_threshold: int = 20
    integrity_repeat_penalty: float = 0.5
    integrity_new_vs_established_discount: float = 0.8
    rating_scale_factor: float = 173.7178
    public_rating_offset: float = 2.0

@dataclass
class PlayerState:
    player_id: str
    mu: float = 1500.0
    phi: float = 350.0
    volatility: float = 0.06
    matches_played: int = 0
    unique_opponents: set = field(default_factory=set)
    opponent_history: Dict[str, List[int]] = field(default_factory=lambda: defaultdict(list))
    last_match_time: int = 0
    status: str = "UNRANKD"
    created_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def public_rating(self) -> float:
        return self.mu - 2.0 * self.phi

    @property
    def public_rating_display(self) -> int:
        return max(0, int(round(self.public_rating)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "player_id": self.player_id,
            "mu": round(self.mu, 4),
            "phi": round(self.phi, 4),
            "volatility": round(self.volatility, 6),
            "matches_played": self.matches_played,
            "unique_opponents": len(self.unique_opponents),
            "status": self.status,
            "public_rating": self.public_rating_display
        }

@dataclass
class RatingEvent:
    event_id: str
    match_id: str
    player_id: str
    algorithm_version: str
    old_rating: int
    new_rating: int
    delta: int
    old_mu: float
    new_mu: float
    old_phi: float
    new_phi: float
    old_volatility: float
    new_volatility: float
    information_weight: float
    integrity_weight: float
    familiarity_factor: float
    opponent_id: str
    score: str
    result: str
    explanation_code: str
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "match_id": self.match_id,
            "player_id": self.player_id,
            "algorithm_version": self.algorithm_version,
            "old_rating": self.old_rating,
            "new_rating": self.new_rating,
            "delta": self.delta,
            "old_mu": round(self.old_mu, 4),
            "new_mu": round(self.new_mu, 4),
            "old_phi": round(self.old_phi, 4),
            "new_phi": round(self.new_phi, 4),
            "old_volatility": round(self.old_volatility, 6),
            "new_volatility": round(self.new_volatility, 6),
            "information_weight": round(self.information_weight, 4),
            "integrity_weight": round(self.integrity_weight, 4),
            "familiarity_factor": round(self.familiarity_factor, 4),
            "opponent_id": self.opponent_id,
            "score": self.score,
            "result": self.result,
            "explanation_code": self.explanation_code,
            "created_at": self.created_at.isoformat()
        }

class RankdEngine:
    def __init__(self, config: Optional[AlgorithmConfig] = None):
        self.config = config or AlgorithmConfig()
        self.players: Dict[str, PlayerState] = {}
        self.rating_events: List[RatingEvent] = []
        self.match_count = 0
        self._processed_matches: set = set()

    def register_player(self, player_id: str, mu: Optional[float] = None,
                       phi: Optional[float] = None) -> PlayerState:
        if player_id not in self.players:
            self.players[player_id] = PlayerState(
                player_id=player_id,
                mu=mu or self.config.initial_mu,
                phi=phi or self.config.initial_phi,
                volatility=0.06
            )
        else:
            # Always update from database to ensure we use latest ratings
            if mu is not None:
                self.players[player_id].mu = mu
            if phi is not None:
                self.players[player_id].phi = phi
        return self.players[player_id]

    def calculate_match(self, player_a_id: str, player_b_id: str,
                       score_a: int, score_b: int,
                       match_id: Optional[str] = None) -> Dict[str, Any]:
        mid = match_id or str(uuid.uuid4())
        idempotency_key = f"{mid}_{self.config.version}"
        if idempotency_key in self._processed_matches:
            raise ValueError(f"Match {mid} already processed")

        self.match_count += 1
        current_time = self.match_count

        p1 = self.register_player(player_a_id)
        p2 = self.register_player(player_b_id)

        p1.opponent_history[p2.player_id].append(current_time)
        p2.opponent_history[p1.player_id].append(current_time)

        fam1 = self._opponent_familiarity(p1, p2.player_id, current_time)
        fam2 = self._opponent_familiarity(p2, p1.player_id, current_time)
        match_info = self._match_information(abs(score_a - score_b), 5)
        integrity = self._integrity_weight(p1, p2, score_a, score_b, current_time)

        s1 = 1.0 if score_a > score_b else 0.0 if score_a < score_b else 0.5
        s2 = 1.0 - s1

        old_a = (p1.mu, p1.phi, p1.volatility, p1.public_rating_display)
        old_b = (p2.mu, p2.phi, p2.volatility, p2.public_rating_display)

        new_mu1, new_phi1, new_vol1 = self._glicko2_update(
            p1.mu, p1.phi, p1.volatility, p2.mu, p2.phi, s1
        )
        new_mu2, new_phi2, new_vol2 = self._glicko2_update(
            p2.mu, p2.phi, p2.volatility, new_mu1, new_phi1, s2
        )

        info1 = match_info * fam1 * integrity
        info2 = match_info * fam2 * integrity
        phi_floor = 30.0

        p1.mu = old_a[0] + (new_mu1 - old_a[0]) * info1
        p1.phi = max(phi_floor, new_phi1)
        p1.volatility = new_vol1

        p2.mu = old_b[0] + (new_mu2 - old_b[0]) * info2
        p2.phi = max(phi_floor, new_phi2)
        p2.volatility = new_vol2

        p1.matches_played += 1
        p2.matches_played += 1
        p1.unique_opponents.add(p2.player_id)
        p2.unique_opponents.add(p1.player_id)
        p1.last_match_time = current_time
        p2.last_match_time = current_time

        self._update_status(p1)
        self._update_status(p2)

        codes = []
        if fam1 < 0.5:
            codes.append("REPEATED_OPPONENT_REDUCED_IMPACT")
        if integrity < 1.0:
            codes.append("INTEGRITY_DISCOUNT_APPLIED")
        if p1.status == "UNRANKD":
            codes.append("PLACEMENT_MATCH")

        event_a = RatingEvent(
            event_id=str(uuid.uuid4()), match_id=mid, player_id=p1.player_id,
            algorithm_version=self.config.version, old_rating=old_a[3],
            new_rating=p1.public_rating_display, delta=p1.public_rating_display - old_a[3],
            old_mu=old_a[0], new_mu=p1.mu, old_phi=old_a[1], new_phi=p1.phi,
            old_volatility=old_a[2], new_volatility=p1.volatility,
            information_weight=info1, integrity_weight=integrity, familiarity_factor=fam1,
            opponent_id=p2.player_id, score=f"{score_a}-{score_b}",
            result="W" if score_a > score_b else "L" if score_a < score_b else "D",
            explanation_code=";".join(codes) if codes else "NORMAL"
        )
        event_b = RatingEvent(
            event_id=str(uuid.uuid4()), match_id=mid, player_id=p2.player_id,
            algorithm_version=self.config.version, old_rating=old_b[3],
            new_rating=p2.public_rating_display, delta=p2.public_rating_display - old_b[3],
            old_mu=old_b[0], new_mu=p2.mu, old_phi=old_b[1], new_phi=p2.phi,
            old_volatility=old_b[2], new_volatility=p2.volatility,
            information_weight=info2, integrity_weight=integrity, familiarity_factor=fam2,
            opponent_id=p1.player_id, score=f"{score_b}-{score_a}",
            result="W" if score_b > score_a else "L" if score_b < score_a else "D",
            explanation_code=";".join(codes) if codes else "NORMAL"
        )

        self.rating_events.extend([event_a, event_b])
        self._processed_matches.add(idempotency_key)

        return {
            "match_id": mid,
            "player_a": {"id": p1.player_id, "new_rating": p1.public_rating_display, "delta": p1.public_rating_display - old_a[3], "status": p1.status},
            "player_b": {"id": p2.player_id, "new_rating": p2.public_rating_display, "delta": p2.public_rating_display - old_b[3], "status": p2.status},
            "info_weight_a": round(info1, 4),
            "info_weight_b": round(info2, 4),
            "explanation_codes": codes
        }

    def _g(self, phi: float) -> float:
        return 1.0 / math.sqrt(1.0 + 3.0 * phi**2 / math.pi**2)

    def _E(self, mu: float, mu_j: float, phi_j: float) -> float:
        return 1.0 / (1.0 + math.exp(-self._g(phi_j) * (mu - mu_j)))

    def _glicko2_update(self, mu: float, phi: float, sigma: float,
                        mu_j: float, phi_j: float, s: float) -> Tuple[float, float, float]:
        mu_g = (mu - 1500) / self.config.rating_scale_factor
        phi_g = phi / self.config.rating_scale_factor
        mu_j_g = (mu_j - 1500) / self.config.rating_scale_factor
        phi_j_g = phi_j / self.config.rating_scale_factor

        g = self._g(phi_j_g)
        E = self._E(mu_g, mu_j_g, phi_j_g)

        v = 1.0 / (g**2 * E * (1 - E))
        delta = v * g * (s - E)

        sigma_new = self._update_volatility(sigma, delta, phi_g, v)
        phi_star = math.sqrt(phi_g**2 + sigma_new**2)
        phi_new = 1.0 / math.sqrt(1.0/phi_star**2 + 1.0/v)
        mu_new = mu_g + phi_new**2 * g * (s - E)

        return (
            1500 + self.config.rating_scale_factor * mu_new,
            self.config.rating_scale_factor * phi_new,
            sigma_new
        )

    def _update_volatility(self, sigma: float, delta: float, phi: float, v: float) -> float:
        a = math.log(sigma**2)
        def f(x):
            ex = math.exp(x)
            return (ex * (delta**2 - phi**2 - v - ex)) / (2.0 * (phi**2 + v + ex)**2) - (x - a) / self.config.tau**2
        A = a
        if delta**2 > phi**2 + v:
            B = math.log(delta**2 - phi**2 - v)
        else:
            k = 1
            while f(a - k * self.config.tau) < 0:
                k += 1
            B = a - k * self.config.tau
        fA, fB = f(A), f(B)
        while abs(B - A) > self.config.epsilon:
            C = A + (A - B) * fA / (fB - fA)
            fC = f(C)
            if fC * fB < 0:
                A, fA = B, fB
            else:
                fA = fA / 2.0
            B, fB = C, fC
        return math.exp(A / 2.0)

    def _opponent_familiarity(self, player: PlayerState, opponent_id: str, current_time: int) -> float:
        history = player.opponent_history.get(opponent_id, [])
        if not history:
            return 1.0
        recent = sum(1 for t in history if current_time - t < self.config.repeat_recovery_time)
        time_since = current_time - history[-1]
        decay = self.config.repeat_decay_base ** recent
        recovery = min(1.0, self.config.repeat_recovery_rate * max(0, time_since - 10))
        familiarity = min(1.0, decay + recovery)
        return max(self.config.opponent_familiarity_floor, familiarity)

    def _match_information(self, score_diff: int, format_max: int) -> float:
        dominance = abs(score_diff) / format_max if format_max > 0 else 0
        info = 1.0 + 0.3 * dominance
        return min(info, self.config.match_info_cap)

    def _integrity_weight(self, p1: PlayerState, p2: PlayerState,
                         s1: int, s2: int, current_time: int) -> float:
        weight = 1.0
        p1_history = len(p1.opponent_history.get(p2.player_id, []))
        p2_history = len(p2.opponent_history.get(p1.player_id, []))
        if p1_history > self.config.integrity_repeat_threshold or \
           p2_history > self.config.integrity_repeat_threshold:
            weight *= self.config.integrity_repeat_penalty
        if p1.matches_played < 5 and p2.matches_played > 50 and s1 > s2:
            weight *= self.config.integrity_new_vs_established_discount
        return max(0.0, min(1.0, weight))

    def _update_status(self, player: PlayerState):
        if player.matches_played < self.config.placement_matches:
            player.status = "UNRANKD"
        elif player.phi > self.config.established_phi_threshold:
            player.status = "PROVISIONAL"
        elif player.matches_played < self.config.established_min_matches:
            player.status = "PROVISIONAL"
        elif len(player.unique_opponents) < self.config.established_min_opponents:
            player.status = "PROVISIONAL"
        else:
            player.status = "ESTABLISHED"
