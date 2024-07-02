from pydantic import BaseModel
from typing import Optional, List
from .player import Player

class Game(BaseModel):
  id: str
  contract_address: Optional[str]
  player1_id: str
  player2_id: str
  player1_address: Optional[str]
  player2_address: Optional[str]
  player1_choice: Optional[str]
  player2_choice: Optional[str]
  player1_wager: Optional[str]
  player2_wager: Optional[str]
  player1_winnings: float = 0
  player2_winnings: float = 0
  player1_losses: float = 0
  player2_losses: float = 0
  player1_wager_accepted: bool = False
  player2_wager_accepted: bool = False
  player1_wager_offered: bool = False
  player2_wager_offered: bool = False
  player1_contract_accepted: bool = False
  player2_contract_accepted: bool = False
  player1_contract_rejected: bool = False
  player2_contract_rejected: bool = False
  player1_rpc_error: bool = False
  player2_rpc_error: bool = False
  player1_wager_refunded: bool = False
  player2_wager_refunded: bool = False
  player1_disconnected: bool = False
  player2_disconnected: bool = False
  player1_nonce: Optional[str]
  player2_nonce: Optional[str]
  winner_id: Optional[str]
  loser_id: Optional[str]
  transactions: List[str] = []
  insufficient_funds: bool = False
  rpc_error: bool = False
  contract_rejected: bool = False
  game_over: bool = False
  uncaught_exception_occured: bool = False

  @property
  def winner(self):
    return self.player1_id if self.winner_id == self.player1_id else self.player2_id if self.winner_id == self.player2_id else None

  @property
  def loser(self):
    return self.player1_id if self.loser_id == self.player1_id else self.player2_id if self.loser_id == self.player2_id else None

  def get_player_data(self, player_id: str) -> Player:
    if player_id == self.player1_id:
      return Player(
        player_id=self.player1_id,
        address=self.player1_address,
        choice=self.player1_choice,
        wager=self.player1_wager,
        winnings=self.player1_winnings,
        losses=self.player1_losses,
        wager_accepted=self.player1_wager_accepted,
        wager_offered=self.player1_wager_offered,
        contract_accepted=self.player1_contract_accepted,
        contract_rejected=self.player1_contract_rejected,
        rpc_error=self.player1_rpc_error,
        wager_refunded=self.player1_wager_refunded,
        player_disconnected=self.player1_disconnected,
        nonce=self.player1_nonce
      )
    elif player_id == self.player2_id:
      return Player(
        player_id=self.player2_id,
        address=self.player2_address,
        choice=self.player2_choice,
        wager=self.player2_wager,
        winnings=self.player2_winnings,
        losses=self.player2_losses,
        wager_accepted=self.player2_wager_accepted,
        wager_offered=self.player2_wager_offered,
        contract_accepted=self.player2_contract_accepted,
        contract_rejected=self.player2_contract_rejected,
        rpc_error=self.player2_rpc_error,
        wager_refunded=self.player2_wager_refunded,
        player_disconnected=self.player2_disconnected,
        nonce=self.player2_nonce
      )
    else:
      raise ValueError(f"Player ID {player_id} not found in this game")

  def update_player_data(self, player_id: str, **kwargs):
    prefix = "player1_" if player_id == self.player1_id else "player2_" if player_id == self.player2_id else None
    if prefix is None:
      raise ValueError(f"Player ID {player_id} not found in this game")
    
    for key, value in kwargs.items():
      setattr(self, f"{prefix}{key}", value)