from pydantic import BaseModel
from typing import Optional, List

class Player(BaseModel):
  player_id: str
  address: Optional[str]
  choice: Optional[str]
  wager: Optional[str]
  winnings: float = 0
  losses: float = 0
  wager_accepted: bool = False
  wager_offered: bool = False
  contract_accepted: bool = False
  contract_rejected: bool = False
  rpc_error: bool = False
  wager_refunded: bool = False
  player_disconnected: bool = False
  nonce: Optional[str]