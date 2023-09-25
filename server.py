import sys
import logging
import uuid
import ssl
import time
import threading
import json
from gevent import monkey
monkey.patch_all()

from flask import Flask, request
from flask_socketio import SocketIO, join_room, emit, leave_room
from flask_cors import CORS

from web3 import Web3
from web3.middleware import geth_poa_middleware
from eth_account import Account

import argparse
from dotenv import load_dotenv
import os

import requests
from decimal import Decimal
import math
import queue

logging.basicConfig(
  stream=sys.stderr,
  level=logging.DEBUG,
  format='%(levelname)s:%(asctime)s:%(message)s'
)

logger = logging.getLogger(__name__)

# Create a parser for the command-line arguments
# e.g. python your_script.py --env .env.prod
parser = argparse.ArgumentParser(description='Loads variables from the specified .env file and prints them.')
parser.add_argument('--env', type=str, default='.env.local', help='The .env file to load')
args = parser.parse_args()
# Load the .env file specified in the command-line arguments
load_dotenv(args.env)
HTTP_PROVIDER = os.getenv("HTTP_PROVIDER")
CONTRACT_OWNER_PRIVATE_KEY = os.getenv("CONTRACT_OWNER_PRIVATE_KEY")
logger.info(f"Contract owner private key: {CONTRACT_OWNER_PRIVATE_KEY}")
ARBITER_PRIVATE_KEY = os.getenv("ARBITER_PRIVATE_KEY")
logger.info(f"Arbiter private key: {ARBITER_PRIVATE_KEY}")
RPS_CONTRACT_FACTORY_ADDRESS = os.getenv("RPS_CONTRACT_FACTORY_ADDRESS")
# Connection
web3 = Web3(Web3.HTTPProvider(HTTP_PROVIDER))

if 'test' in args.env:
  logger.info("Running on testnet")
  # # for Goerli, make sure to inject the poa middleware
  web3.middleware_onion.inject(geth_poa_middleware, layer=0)

# Load and parse the contract ABIs.
rps_contract_factory_abi = None
rps_contract_abi = None
with open('contracts/RPSContractFactory.json') as f:
  factory_json = json.load(f)
  rps_contract_factory_abi = factory_json['abi']
with open('contracts/RPSContract.json') as f:
  rps_json = json.load(f)
  rps_contract_abi = rps_json['abi']

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
CORS(app)
socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins='*')
games = {}
player_queue = queue.Queue()
players = {}

def determine_winner(player1, player2):
  player1_choice = player1['choice']
  player2_choice = player2['choice']
  if player1_choice == player2_choice:
    return None, None
  elif (player1_choice == 'rock' and player2_choice == 'scissors') or \
    (player1_choice == 'paper' and player2_choice == 'rock') or \
    (player1_choice == 'scissors' and player2_choice == 'paper'):
    return player1, player2
  else:
    return player2, player1

def get_new_game():
  game_id = uuid.uuid4()
  
  new_game = {
    'id': game_id,
    'player1': None,
    'player2': None,
    'winner': None,
    'loser': None,
    'contract_address': None,
    'transactions': []
  }
  """
  transaction object
  {
    'confirmationNumber': None,
    'transactionHash': None,
    'receipt': None
  }
  """
  return new_game

def get_new_player():
  return {
    'game_id': None,
    'player_address': None,
    'choice': None,
    'wager': None,
    'winnings': 0,
    'losses': 0,
    'wager_accepted': False,
    'wager_offered': False
  }

@socketio.on('transaction_rejected')
def handle_transaction_rejected(data):
  game = games[data['game_id']]
  # determine which player rejected the contract
  # notify the other player that the contract was rejected
  if game['player1']['player_address'] == data['player_address']:
    emit('transaction_rejected', data, room=game['player2']['player_address'])
  else:
    emit('transaction_rejected', data, room=game['player1']['player_address'])

  # will need to call the contract to refund the player that did not reject the contract

  logger.info('%s decided to reject the contract.', data['player_address'])

@socketio.on('join_contract_confirmation_number_received')
def handle_join_contract_confirmation_number_received(data):
  logger.info('join contract confirmation number: %s', data)

@socketio.on('join_contract_transaction_hash_received')
def handle_join_contract_transaction(data):
  logger.info('join contract transaction hash: %s', data)
  game = games[data['game_id']]
  logger.info(f"Game: {game}")
  game['transactions'].append(data['transaction_hash'])
  logger.info(f"Current game state in on join_contract_transaction_hash_received: {game}")

@socketio.on('join_contract_transaction_receipt_received')
def handle_join_contract_transaction(data):
  logger.info('join contract transaction receipt received: %s', data)  

@socketio.on('connect')
def handle_connect():
  params = request.args
  player_address = params.get('player_address')
  
  # create a new player
  player = get_new_player()
  player['player_address'] = player_address
  # add the player to the queue
  player_queue.put(player)

def join_game():
  # try to remove the first two players from the queue
  try:
    player1 = player_queue.get(block=False)
  except queue.Empty:
    logger.info('Player queue is empty.')
    return
  try:
    player2 = player_queue.get(block=False)
  except queue.Empty:
    # put the first player back in the queue
    player_queue.put(player1)
    logger.info('Player queue has less than two players.')
    return
  # if both players have the same address, return
  try:
    player1 = player_queue.get(block=False)
    player2 = player_queue.get(block=False)
    if player1['player_address'] == player2['player_address']:
      return
  except queue.Empty:
    # put the first player back in the queue
    player_queue.put(player1)
    logger.info('Player queue has less than two players.')
    return    
  # if either player has already joined a game, return
  try:
    player1 = player_queue.get(block=False)
    player2 = player_queue.get(block=False)
    for game in games.values():
      if game['player1']['player_address'] == player1['player_address']:
        return
      if game['player2']['player_address'] == player2['player_address']:
        return
  except queue.Empty:
    # put the first player back in the queue
    player_queue.put(player1)
    logger.info('Player queue has less than two players.')
    return    
  # create a new game
  game = get_new_game()
  # set the player1 and player2
  game['player1'] = player1
  game['player2'] = player2
  # add the game to the games dictionary
  games[game['id']] = game
  # emit an event to both players to inform them that the game has started
  logger.info('Game started. Player queue: {}'.format(player_queue))
  logger.info('Games: {}'.format(games))
  emit('game_started', {'game_id': game['id']}, room=game['player1']['player_address'])
  emit('game_started', {'game_id': game['id']}, room=game['player2']['player_address'])

@socketio.on('offer_wager')
def handle_submit_wager(data):
  player_address = data['player_address']
  logger.info('Received wager from address %s', player_address)
  wager = data['wager']

  # get the game session from the games dictionary
  game = games[data['game_id']]
  # assign the wager to the correct player  
  # if player1 offered the wager, emit an event to player2 to inform them that player1 offered a wager
  if player_address == game['player1']['player_address']:
    game['player1']['wager'] = wager
    emit('wager_offered', {'wager': wager}, room=game['player1']['player_address'])
  else:
    game['player2']['wager'] = wager
    emit('wager_offered', {'wager': wager}, room=game['player2']['player_address'])

@socketio.on('accept_wager')
def handle_accept_wager(data):
  player_address = data['player_address']
  # get the game session from the games dictionary
  game = games[data['game_id']]
  # mark the player as having accepted the wager
  if player_address == game['player1']['player_address']:
    game['player1']['wager_accepted'] = True
  else:
    game['player2']['wager_accepted'] = True

  if game['player1']['wager_accepted'] and game['player2']['wager_accepted']:
    logger.info('Both players have accepted a wager. Generating contract.')
    # emit an event to both players to inform them that the contract is being generated
    emit('generating_contract', room=game['player1']['player_address'])
    emit('generating_contract', room=game['player2']['player_address'])

    tx_hash = None
    arbiter_fee_percentage = int(6.5 * 10**2) # 6.5%
    contract_owner_account = Account.from_key(CONTRACT_OWNER_PRIVATE_KEY)
    logger.info(f"Contract owner address: {contract_owner_account.address}")

    # Create contract using createContract function
    # Use your deployed factory contract address
    factory_contract_address = web3.to_checksum_address(RPS_CONTRACT_FACTORY_ADDRESS)
    # Now interact with your factory contract
    factory_contract = web3.eth.contract(address=factory_contract_address, abi=rps_contract_factory_abi)

    if 'local' in args.env:
      # running locally using ganache
      logger.info("Running locally using ganache")
      tx_hash = factory_contract.functions.createContract(arbiter_fee_percentage).transact({
        'from': web3.to_checksum_address(contract_owner_account.address)
      })
    else:
      # running on the Goerli testnet
      logger.info("Running on Goerli testnet")
      # # for Goerli, make sure to inject the poa middleware
      # web3.middleware_onion.inject(geth_poa_middleware, layer=0)
      # Set Gas Price
      gas_price = web3.eth.gas_price  # Fetch the current gas price

      # Sign transaction using the private key of the owner account
      nonce = web3.eth.get_transaction_count(contract_owner_account.address)  # Get the nonce

      # Create contract using createContract function      
      construct_txn = factory_contract.functions.createContract(
        arbiter_fee_percentage
      ).build_transaction({
        'from': web3.to_checksum_address(contract_owner_account.address),
        'nonce': nonce,
        'gas': 5000000,  # You may need to change the gas limit
        'gasPrice': math.ceil(gas_price * 1.05)
      })

      signed = contract_owner_account.sign_transaction(construct_txn)
      tx_hash = web3.eth.send_raw_transaction(signed.rawTransaction)
      
    tx_receipt = None

    # Get the transaction receipt for the contract creation transaction
    tx_receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
    game['transactions'].append(tx_receipt)
    logger.info(f"Transaction receipt: {tx_receipt}")
    
    # Call getContracts function
    contract_addresses = factory_contract.functions.getContracts().call()
    logger.info(f"Contract addresses: {contract_addresses}")
    # Call the getLatestContract function
    created_contract_address = factory_contract.functions.getLatestContract().call()  
    logger.info(f"Created contract address: {created_contract_address}")
    game['contract_address'] = created_contract_address  

    # notify both players that the contract has been created
    emit('contract_created', {
      'contract_address': created_contract_address, 
      'your_wager': game['player1']['wager'],
      'opponent_wager': game['player2']['wager'],
    }, room=game['player1']['address'])
    emit('contract_created', {
      'contract_address': created_contract_address,
      'your_wager': game['player2']['wager'],
      'opponent_wager': game['player1']['wager'],
    }, room=game['player2']['address'])
  else:
    logger.info('Player %s accepted the wager. Waiting for opponent.', player_address)
    # if player1_id accepted the wager, emit an event to player2 to inform them that player1 accepted the wager and visa versa
    if player_address == game['player1']['player_address']:
      emit('wager_accepted', room=game['player1']['player_address'])
    else:
      emit('wager_accepted', room=game['player1']['player_address'])

@socketio.on('decline_wager')
def handle_decline_wager(data):
  player_address = data['player_address']
  # get the game session from the games dictionary
  game = games[data['game_id']]
  # mark the player as having declined the wager
  if game['player1']['player_address'] == player_address:
    game['player1']['wager_accepted'] = False
    # emit wager declined event to the opposing player
    emit('wager_declined', room=game['player2']['player_address'])
  else:
    game['player2']['wager_accepted'] = False
    # emit wager declined event to the opposing player
    emit('wager_declined', room=game['player1']['player_address'])

@socketio.on('choice')
def handle_choice(data):
  player_address = data['player_address']
  game = games[data['game_id']]
  # assign the choice to the player
  if game['player1']['player_address'] == player_address:
    game['player1']['choice'] = data['choice']
  else:
    game['player2']['choice'] = data['choice']

  logger.info('Player {} chose: {}'.format(data['player_address'], data['choice']))

  if game['player1']['choice'] and game['player2']['choice']:
    winner, loser = determine_winner(game['player1'], game['player2'])
    winner_address = None

    arbiter_account = Account.from_key(ARBITER_PRIVATE_KEY)
    logger.info(f"Arbiter account address: {arbiter_account.address}")
    
    if winner is None and loser is None:
      logger.info('Game is a draw.')
      # set the game winner and loser
      game['winner'] = None
      game['loser'] = None

      winner_address = arbiter_account.address    
    else:
      game['winner'] = winner
      game['loser'] = loser
      logger.info('Winning player: {}'.format(winner))
      logger.info('Losing player: {}'.format(loser))
      # assign winnings to the winning player
      game['winner']['winnings'] = int(loser['wager'])
      # assign losses to the losing player
      game['loser']['losses'] = int(loser['wager'])

      winner_address = winner['address']

    # call decideWinner contract function here
    logger.info('Calling decideWinner contract function')
    
    logger.info(f"Winner account address: {winner_address}")
    rps_contract_address = web3.to_checksum_address(game['contract_address'])
    logger.info(f"Contract address: {rps_contract_address}")
    
    # Now interact with the rps contract
    rps_contract = web3.eth.contract(address=rps_contract_address, abi=rps_contract_abi)

    tx_hash = None
    rps_txn = None

    # Set Gas Price
    gas_price = web3.eth.gas_price  # Fetch the current gas price
    arbiter_checksum_address = web3.to_checksum_address(arbiter_account.address)

    # Sign transaction using the private key of the arbiter account
    nonce = web3.eth.get_transaction_count(arbiter_checksum_address)  # Get the nonce

    if 'local' in args.env:
      # running locally using ganache
      logger.info("Running locally using ganache")
    else:
      # running on the Goerli testnet
      logger.info("Running on Goerli testnet")

    rps_txn = rps_contract.functions.decideWinner(web3.to_checksum_address(winner_address)).build_transaction({
      'from': arbiter_checksum_address,
      'nonce': nonce,
      'gas': 800000,  # You may need to change the gas limit
      'gasPrice': math.ceil(gas_price * 1.05)
    })

    signed = arbiter_account.sign_transaction(rps_txn)
    tx_hash = web3.eth.send_raw_transaction(signed.rawTransaction)
    
    tx_receipt = None

    # Get the transaction receipt for the decide winner transaction
    tx_receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
    print(f'Transaction receipt after decideWinner was called: {tx_receipt}')
    game['transactions'].append(tx_receipt)

    if winner is None and loser is None:
      # emit an event to both players to inform them that the game is a draw
      emit('draw', {
        'your_choice': game['player1']['choice'],
        'opp_choice': game['player2']['choice']}, room=game['player1']['address'])
      emit('draw', {
        'your_choice': game['player2']['choice'],
        'opp_choice': game['player1']['choice']}, room=game['player2']['choice'])
    else:
      # emit an event to both players to inform them of the result
      emit('you_win', {
        'your_choice': game['winner']['choice'],
        'opp_choice': game['loser']['choice'],
        'winnings': game['winner']['winnings']
        }, room=game['winner']['address'])
      emit('you_lose', {
        'your_choice': game['loser']['choice'],
        'opp_choice': game['winner']['choice'], 
        'losses': game['loser']['losses']
        }, room=game['loser']['address'])

@socketio.on('disconnect')
def handle_disconnect():
  # the `request` context still contains the disconnection information
  player_address = request.args.get('player_address')
  # find the player in the games dictionary
  for game in games.values():
    if game['player1']['player_address'] == player_address:
      # emit an event to the other player to inform them that the player disconnected
      emit('opponent_disconnected', room=game['player2']['player_address'])
      # remove the game from the games dictionary
      del games[game['id']]
      break
    elif game['player2']['player_address'] == player_address:
      # emit an event to the other player to inform them that the player disconnected
      emit('opponent_disconnected', room=game['player1']['player_address'])
      # remove the game from the games dictionary
      del games[game['id']]
      break
 
@socketio.on('join_game')        
def join_game_loop():
  if player_queue.qsize() >= 2:
    join_game()
  else:
    logger.info('Waiting for players to join. Player queue: {}'.format(player_queue))
    logger.info('Games: {}'.format(games))

if __name__ == '__main__':
  from geventwebsocket.handler import WebSocketHandler
  from gevent.pywsgi import WSGIServer

  logger.info('Starting server...')

  http_server = WSGIServer(('0.0.0.0', 443),
                           app,
                           keyfile='/etc/letsencrypt/live/dev.generalsolutions43.com/privkey.pem',
                           certfile='/etc/letsencrypt/live/dev.generalsolutions43.com/fullchain.pem',
                           handler_class=WebSocketHandler)

  # http_server = WSGIServer(('0.0.0.0', 8000), app, handler_class=WebSocketHandler)

  http_server.serve_forever()