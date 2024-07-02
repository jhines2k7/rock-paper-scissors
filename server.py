from typing import Optional
from gevent import monkey
monkey.patch_all()

import datetime
import sys
import logging
import uuid
import json
import argparse
import os
import queue
import binascii
import requests
import time
import threading
import sqlite3

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2 import service_account
from googleapiclient.http import MediaIoBaseDownload
from flask import Flask, jsonify, request
from flask_socketio import SocketIO, join_room, emit, leave_room
from flask_cors import CORS
from web3 import Web3
from web3.middleware import geth_poa_middleware
from eth_account import Account
from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler
from datetime import datetime
from healthcheck import HealthCheck
from decimal import ROUND_DOWN, Decimal
from models import Game, Player
from contextlib import contextmanager

logging.basicConfig(
  stream=sys.stderr,
  level=logging.DEBUG,
  format='%(levelname)s:%(asctime)s:%(message)s'
)

logger = logging.getLogger(__name__)

# Creating a logger that will write transaction hashes to a file
txn_logger = logging.getLogger('txn_logger')
txn_logger.setLevel(logging.CRITICAL)

# Creates a file handler which writes DEBUG messages or higher to the file
# Get current date and time
now = datetime.now()

# Format datetime string to be used in the filename
dt_string = now.strftime("%d_%m_%Y_%H_%M_%S")

log_file_name = 'txn_hashes_{}.log'.format(dt_string)
logger.info(f"Log file name: {log_file_name}")
log_file_handler = RotatingFileHandler('logs/' + log_file_name, maxBytes=1e6, backupCount=50)
log_file_handler.setLevel(logging.CRITICAL)

# Creates a formatter and adds it to the handler
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log_file_handler.setFormatter(formatter)

# Adds the handler to the logger
txn_logger.addHandler(log_file_handler)

DATABASE_NAME = 'rps_game.db'

@contextmanager
def get_db_connection():
  conn = sqlite3.connect(DATABASE_NAME)
  try:
    yield conn
  finally:
    conn.close()

def init_db():
  with get_db_connection() as conn:
    cursor = conn.cursor()
    cursor.execute('''
      CREATE TABLE IF NOT EXISTS games (
        id TEXT PRIMARY KEY,
        contract_address TEXT,
        player1_id TEXT,
        player2_id TEXT,
        player1_address TEXT,
        player2_address TEXT,
        player1_choice TEXT,
        player2_choice TEXT,
        player1_wager TEXT,
        player2_wager TEXT,
        player1_winnings REAL,
        player2_winnings REAL,
        player1_losses REAL,
        player2_losses REAL,
        player1_wager_accepted BOOLEAN,
        player2_wager_accepted BOOLEAN,
        player1_wager_offered BOOLEAN,
        player2_wager_offered BOOLEAN,
        player1_contract_accepted BOOLEAN,
        player2_contract_accepted BOOLEAN,
        player1_contract_rejected BOOLEAN,
        player2_contract_rejected BOOLEAN,
        player1_rpc_error BOOLEAN,
        player2_rpc_error BOOLEAN,
        player1_wager_refunded BOOLEAN,
        player2_wager_refunded BOOLEAN,
        player1_disconnected BOOLEAN,
        player2_disconnected BOOLEAN,
        player1_nonce TEXT,
        player2_nonce TEXT,
        winner_id TEXT,
        loser_id TEXT,
        transactions TEXT,
        insufficient_funds BOOLEAN,
        rpc_error BOOLEAN,
        contract_rejected BOOLEAN,
        game_over BOOLEAN,
        uncaught_exception_occured BOOLEAN
      )
    ''')
    conn.commit()

# Call this function at the start of your script
init_db()

# Create a parser for the command-line arguments
# e.g. python your_script.py --env .env.prod
parser = argparse.ArgumentParser(description='Loads variables from the specified .env file and prints them.')
parser.add_argument('--env', type=str, default='.env.sepolia', help='The .env file to load')
args = parser.parse_args()
# Load the .env file specified in the command-line arguments
load_dotenv(args.env)
HTTP_PROVIDER = os.getenv("HTTP_PROVIDER")
CONTRACT_OWNER_PRIVATE_KEY = os.getenv("CONTRACT_OWNER_PRIVATE_KEY")
RPS_CONTRACT_ADDRESS = os.getenv("RPS_CONTRACT_ADDRESS")
logger.info(f"RPS contract address: {RPS_CONTRACT_ADDRESS}")
KEYFILE = os.getenv("KEYFILE")
CERTFILE = os.getenv("CERTFILE")
COINGECKO_API = os.getenv("COINGECKO_API")
GAS_ORACLE_API_KEY = os.getenv("GAS_ORACLE_API_KEY")
COSMOS_ENDPOINT = os.getenv("COSMOS_ENDPOINT")
COSMOS_KEY = os.getenv("COSMOS_KEY")
COSMOS_DB_NAME = os.getenv("COSMOS_DB_NAME")
COSMOS_CONTAINER_NAME = os.getenv("COSMOS_CONTAINER_NAME")
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
AZURE_QUEUE_NAME = os.getenv("AZURE_QUEUE_NAME")
COSMOS_PARTITION_KEY = os.getenv("COSMOS_PARTITION_KEY")

# Connection
web3 = Web3(Web3.HTTPProvider(HTTP_PROVIDER))
if 'sepolia' in args.env or 'mainnet' in args.env:
  web3.middleware_onion.inject(geth_poa_middleware, layer=0)

if 'ganache' in args.env:
  logger.info("Running on Ganache testnet")
elif 'sepolia' in args.env:
  logger.info("Running on Sepolia testnet")
elif 'mainnet' in args.env:
  logger.info("Running on Ethereum mainnet")

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY")
CORS(app)
socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins='*')

rps_contract_factory_abi = None
rps_contract_abi = None
cosmos_db = None
gas_oracles = []
ethereum_prices = []

nonce_lookup = {}

health = HealthCheck()
app.add_url_rule("/healthcheck", "healthcheck", view_func=lambda: health.run())

# Create an in-memory queue to store waiting players
player_queue = queue.Queue()

def eth_to_usd(eth_balance):
  latest_price = ethereum_prices[-1] #get_eth_price()
  eth_price = Decimal(latest_price)  # convert the result to Decimal
  return eth_balance * eth_price

def get_gas_oracle():
  url = "https://api.etherscan.io/api"
  payload = {
    'module': 'gastracker',
    'action': 'gasoracle',
    'apikey': GAS_ORACLE_API_KEY
  }

  for _ in range(5):
    try:
      response = requests.get(url, params=payload)
      response.raise_for_status()
      return response.json()
    except (requests.exceptions.RequestException, KeyError):
      time.sleep(12)

    raise Exception("Failed to fetch gas price after several attempts")

def get_eth_price():
  for _ in range(5):
    try:
      response = requests.get(COINGECKO_API)
      response.raise_for_status()  # Raise an exception if the request was unsuccessful
      data = response.json()
      return data['ethereum']['usd']
    except (requests.exceptions.RequestException, KeyError):
      time.sleep(12)

    # If we've gotten to this point, all the retry attempts have failed
    raise Exception("Failed to fetch Ethereum price after several attempts")

def usd_to_eth(usd):
  eth_price = ethereum_prices[-1] #get_eth_price()
  return usd / eth_price

def convert_bytes_to_hex(bytes_value):
  hex_value = binascii.b2a_hex(bytes_value)
  hex_str = hex_value.decode()  # Convert bytes to str

  # Add '0x' at the beginning to indicate that it's a hex number
  hex_str = '0x' + hex_str

  return hex_str

def get_service(api_name, api_version, scopes, key_file_location):
  """Get a service that communicates to a Google API.

  Args:
    api_name: The name of the api to connect to.
    api_version: The api version to connect to.
    scopes: A list auth scopes to authorize for the application.
    key_file_location: The path to a valid service account JSON key file.

  Returns:
    A service that is connected to the specified API.
  """

  credentials = service_account.Credentials.from_service_account_file(
  key_file_location)

  scoped_credentials = credentials.with_scopes(scopes)

  # Build the service object.
  service = build(api_name, api_version, credentials=scoped_credentials)

  return service

def download_contract_abi():
  # Define the auth scopes to request.
  scope = 'https://www.googleapis.com/auth/drive.file'
  key_file_location = 'service-account.json'
    
  # Specify the name of the folder you want to retrieve
  folder_name = 'rock-paper-scissors-v2'
  
  try:
    # Authenticate and construct service.
    service = get_service(
      api_name='drive',
      api_version='v3',
      scopes=[scope],
      key_file_location=key_file_location)
        
    results = service.files().list(q=f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder'").execute()
    folders = results.get('files', [])
    folder_id = None

    # Print the folder's ID if found
    if len(folders) > 0:
      logger.info(f"Folder ID: {folders[0]['id']}")
      logger.info(f"Folder name: {folders[0]['name']}")
      folder_id = folders[0]['id']
    else:
      logger.info("Folder not found.")

    # Delete all files in the local contracts folder
    download_dir = 'contracts'
    logger.info('Deleting all files in the contracts folder...')
    for file in os.listdir(download_dir):
      file_path = os.path.join(download_dir, file)
      try:
        if os.path.isfile(file_path):
          os.unlink(file_path)
      except Exception as e:
        logger.error(f"An error occurred while deleting file: {file_path}")
        logger.error(e)
    # Getting all files in the contracts folder
    results = service.files().list(q=f"'{folder_id}' in parents and trashed=false", pageSize=1000, fields="nextPageToken, files(id, name, createdTime)").execute()
    files = results.get('files', [])
    
    logger.info('Downloading contract ABIs...')
    # Download each file from the folder
    for file in files:
      request_file = service.files().get_media(fileId=file['id'])
      # Get the file metadata
      file_metadata = service.files().get(fileId=file['id']).execute()
      file_name = file_metadata['name']
      created_time = datetime.strptime(file['createdTime'], "%Y-%m-%dT%H:%M:%S.%fZ")
      logger.info(f"File Name: {file_name}, Created Time: {created_time}")

      # Download the file content
      fh = open(os.path.join("contracts", file_name), 'wb')
      downloader = MediaIoBaseDownload(fh, request_file)

      done = False
      while done is False:
        status, done = downloader.next_chunk()
        logger.info(f"Download progress {int(status.progress() * 100)}%.")

      print('File downloaded successfully.')

    logger.info('Contract ABIs downloaded successfully!')

    # Load and parse the contract ABIs.
    with open('contracts/RPSContractV2.json') as f:
      global rps_contract_abi
      rps_json = json.load(f)
      rps_contract_abi = rps_json['abi']

  except HttpError as error:
    # TODO(developer) - Handle errors from drive API.
    logger.error(f'An error occurred: {error}')

player1_verified = False
player2_verified = False
player1_nonce = None
player2_nonce = None

def save_new_game_to_db(game: Game) -> Game:
  with get_db_connection() as conn:
    cursor = conn.cursor()
    cursor.execute('''
      INSERT INTO games (
        id, contract_address, player1_id, player2_id,
        player1_address, player2_address, player1_choice, player2_choice,
        player1_wager, player2_wager, player1_winnings, player2_winnings,
        player1_losses, player2_losses, player1_wager_accepted, player2_wager_accepted,
        player1_wager_offered, player2_wager_offered, player1_contract_accepted,
        player2_contract_accepted, player1_contract_rejected, player2_contract_rejected,
        player1_rpc_error, player2_rpc_error, player1_wager_refunded, player2_wager_refunded,
        player1_disconnected, player2_disconnected, player1_nonce, player2_nonce,
        winner_id, loser_id, transactions, insufficient_funds, rpc_error,
        contract_rejected, game_over, uncaught_exception_occured
      ) VALUES (
        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
      )
    ''', (
      game.id, game.contract_address, game.player1_id, game.player2_id,
      game.player1_address, game.player2_address, game.player1_choice, game.player2_choice,
      game.player1_wager, game.player2_wager, game.player1_winnings, game.player2_winnings,
      game.player1_losses, game.player2_losses, game.player1_wager_accepted, game.player2_wager_accepted,
      game.player1_wager_offered, game.player2_wager_offered, game.player1_contract_accepted,
      game.player2_contract_accepted, game.player1_contract_rejected, game.player2_contract_rejected,
      game.player1_rpc_error, game.player2_rpc_error, game.player1_wager_refunded, game.player2_wager_refunded,
      game.player1_disconnected, game.player2_disconnected, game.player1_nonce, game.player2_nonce,
      game.winner_id, game.loser_id, json.dumps(game.transactions), game.insufficient_funds, game.rpc_error,
      game.contract_rejected, game.game_over, game.uncaught_exception_occured
    ))
    conn.commit()
  
  # After inserting, fetch the game to ensure we return the exact state as stored in the database
  return get_game(game.id)

def get_game(game_id: str) -> Game:
  with get_db_connection() as conn:
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM games WHERE id = ?', (game_id,))
    row = cursor.fetchone()
    if row:
      # Convert row to dictionary
      columns = [column[0] for column in cursor.description]
      game_dict = dict(zip(columns, row))
      
      # Convert transactions from JSON string to list
      game_dict['transactions'] = json.loads(game_dict['transactions'])
      
      # Create and return Game object
      return Game(**game_dict)
  return None
  
def update_game(game: Game) -> Optional[Game]:
  with get_db_connection() as conn:
    cursor = conn.cursor()
    cursor.execute('''
      UPDATE games SET
        contract_address = ?,
        player1_id = ?, player2_id = ?,
        player1_address = ?, player2_address = ?,
        player1_choice = ?, player2_choice = ?,
        player1_wager = ?, player2_wager = ?,
        player1_winnings = ?, player2_winnings = ?,
        player1_losses = ?, player2_losses = ?,
        player1_wager_accepted = ?, player2_wager_accepted = ?,
        player1_wager_offered = ?, player2_wager_offered = ?,
        player1_contract_accepted = ?, player2_contract_accepted = ?,
        player1_contract_rejected = ?, player2_contract_rejected = ?,
        player1_rpc_error = ?, player2_rpc_error = ?,
        player1_wager_refunded = ?, player2_wager_refunded = ?,
        player1_disconnected = ?, player2_disconnected = ?,
        player1_nonce = ?, player2_nonce = ?,
        winner_id = ?, loser_id = ?,
        transactions = ?,
        insufficient_funds = ?, rpc_error = ?,
        contract_rejected = ?, game_over = ?,
        uncaught_exception_occured = ?
      WHERE id = ?
    ''', (
      game.contract_address,
      game.player1_id, game.player2_id,
      game.player1_address, game.player2_address,
      game.player1_choice, game.player2_choice,
      game.player1_wager, game.player2_wager,
      game.player1_winnings, game.player2_winnings,
      game.player1_losses, game.player2_losses,
      game.player1_wager_accepted, game.player2_wager_accepted,
      game.player1_wager_offered, game.player2_wager_offered,
      game.player1_contract_accepted, game.player2_contract_accepted,
      game.player1_contract_rejected, game.player2_contract_rejected,
      game.player1_rpc_error, game.player2_rpc_error,
      game.player1_wager_refunded, game.player2_wager_refunded,
      game.player1_disconnected, game.player2_disconnected,
      game.player1_nonce, game.player2_nonce,
      game.winner_id, game.loser_id,
      json.dumps(game.transactions),
      game.insufficient_funds, game.rpc_error,
      game.contract_rejected, game.game_over,
      game.uncaught_exception_occured,
      game.id
    ))
    conn.commit()

  # After updating, fetch the game to ensure we return the exact state as stored in the database
  return get_game(game.id)

@socketio.on('nonce_supplied')
def handle_nonce_supplied(data):
  
  game = get_game(data['game_id'])

  rps_contract_address = web3.to_checksum_address(RPS_CONTRACT_ADDRESS)
  logger.info(f"Contract address: {rps_contract_address}")
  
  global rps_contract_abi
  rps_contract = web3.eth.contract(address=rps_contract_address, abi=rps_contract_abi)

  nonce = data['player_nonce']

  player = None
  
  if data['player_id'] == game.player1_id:
    logger.info(f"Player1 nonce {nonce}")
    global player1_nonce
    player1_nonce = nonce
    player = game.player1
  else:
    logger.info(f"Player2 nonce {nonce}")
    global player2_nonce
    player2_nonce = nonce
    player = game.player2

  player_move_hash = rps_contract.functions.getPlayerMoveHash(game.id, player.address).call()
  logger.info(f"Player {player.player_id} move hash: {player_move_hash}")

  verified = verify_move(game_id=game.id, 
                                  address=player.address, 
                                  choice=player.choice, 
                                  nonce=nonce, 
                                  hash=player_move_hash)
  logger.info(f"Player {player.player_id} move verified: {verified}")
  
  if data['player_id'] == game.player1_id:
    global player1_verified
    player1_verified = verified
  else:
    global player2_verified
    player2_verified = verified

  if player1_verified and player2_verified:
    game.player1.nonce = player1_nonce
    game.player2.nonce = player2_nonce
    current_game = update_game(game)
    logger.info(f"Current game state in on nonce_supplied: {current_game}")

    logger.info(f"Player1 verified: {player1_verified}, Player2 verified: {player2_verified}")
    
    settle_game(game.id)

def verify_move(game_id, address, choice, nonce, hash):
  player_move = {
    'player_address': address,
    'game_id': game_id,
    'choice': choice,
  }

  # convert the player_move to a string
  player_move_str = json.dumps(player_move)
  # add the nonce to the player_move string
  player_move_str += str(nonce)
  logger.info(f"Player move string: {player_move_str.replace(' ', '')}")
  # use web3 to hash the player_move string
  player_move_hash = web3.solidity_keccak(['string'], [player_move_str.replace(' ', '')])
  logger.info(f"Player move hash from call to web3.solidity_keccak: {player_move_hash.hex()}")
  logger.info(f"Hash from contract: {hash}")

  if player_move_hash.hex() == hash:
    return True
  else:
    return False

def determine_winner(game: Game) -> Game:
  player1_choice = game.player1_choice
  player2_choice = game.player2_choice
  if player1_choice == player2_choice:
    return None, None
  elif (player1_choice == 'rock' and player2_choice == 'scissors') or \
    (player1_choice == 'paper' and player2_choice == 'rock') or \
    (player1_choice == 'scissors' and player2_choice == 'paper'):
    game.winner_id = game.player1_id
    game.loser_id = game.player2_id
    
    return update_game(game)
  else:
    return game
  
def refund_wager(game:Game, payee:Player) -> Optional[str]:  
  # will need to refund the player that did not reject the contract
  contract_owner_account = Account.from_key(CONTRACT_OWNER_PRIVATE_KEY)
  logger.info(f"Contract owner account address: {contract_owner_account.address}")

  # call refundWager contract function here
  logger.info('Calling refundWager contract function')
  logger.info(f"Payee: {payee}")
  
  logger.info(f"Payee account address: {payee.address}")
  rps_contract_address = web3.to_checksum_address(RPS_CONTRACT_ADDRESS)
  logger.info(f"Contract address: {rps_contract_address}")
  
  # Now interact with the rps contract
  global rps_contract_abi
  rps_contract = web3.eth.contract(address=rps_contract_address, abi=rps_contract_abi)

  tx_hash = None
  rps_txn = None

  gas_oracle = gas_oracles[-1] # get_gas_oracle()

  fast_gas_price = float(gas_oracle['result']['FastGasPrice'])
  logger.info(f"Fast gas price for payWinner: {fast_gas_price}")

  suggest_base_fee = float(gas_oracle['result']['suggestBaseFee'])
  logger.info(f"Suggested base fee for payWinner: {suggest_base_fee}")

  max_priority_fee_per_gas = round(fast_gas_price) - round(suggest_base_fee)
  logger.info(f"Max priority fee per gas: {max_priority_fee_per_gas}")

  contract_owner_checksum_address = web3.to_checksum_address(contract_owner_account.address)

  # Sign transaction using the private key of the arbiter account
  nonce = web3.eth.get_transaction_count(contract_owner_checksum_address)
  
  if 'sepolia' in args.env:
    # running on the Sepolia testnet
    logger.info("Running on Sepolia testnet")
  elif 'mainnet' in args.env:
    # running on the Ethereum mainnet
    logger.info("Running on Ethereum mainnet")

  refund_in_ether = usd_to_eth(float(payee.wager.replace('$', '')))
  logger.info(f"Refund in ether: {refund_in_ether}")
  refund_in_wei = web3.to_wei(refund_in_ether, 'ether')
  logger.info(f"Refund in wei: {refund_in_wei}")

  refund_fee = refund_in_ether * 0.02
  logger.info(f"Refund fee: {refund_fee}")

  gas_estimate_txn = rps_contract.functions.refundWager(web3.to_checksum_address(payee.address),
                                                        refund_in_wei,
                                                        web3.to_wei(refund_fee, 'ether'),
                                                        game.id
    ).build_transaction({
    'from': contract_owner_checksum_address,
    'nonce': nonce
  })

  gas_estimate = web3.eth.estimate_gas(gas_estimate_txn)
  logger.info(f"Gas estimate for refund wager txn: {gas_estimate}")

  # Calculate the estimated transaction cost in wei
  est_total_cost_wei = gas_estimate * web3.to_wei(gas_oracle['result']['SafeGasPrice'], 'gwei')

  # Convert wei to ether, if needed
  est_total_cost_eth = web3.from_wei(est_total_cost_wei, 'ether')
  logger.info(f'Estimated total cost in ether to refund wager: {est_total_cost_eth}')

  est_total_cost_usd = eth_to_usd(Decimal(str(est_total_cost_eth)))
  logger.info(f'Estimated total cost in USD to refund wager: {est_total_cost_usd}')
  txn_logger.critical(f"Estimated total cost in USD to refund wager: {est_total_cost_usd}")

  gas_fee_premium = 1.12 * float(est_total_cost_eth)

  rps_txn = rps_contract.functions.refundWager(web3.to_checksum_address(payee.address), 
                                                        refund_in_wei,
                                                        web3.to_wei(str(gas_fee_premium), 'ether'),
                                                        game.id).build_transaction({
    'from': contract_owner_checksum_address,
    'gas': gas_estimate,
    'nonce': nonce,
    'maxFeePerGas': web3.to_wei(gas_oracle['result']['SafeGasPrice'], 'gwei'),
    'maxPriorityFeePerGas': web3.to_wei(str(max_priority_fee_per_gas), 'gwei')
  })

  signed = contract_owner_account.sign_transaction(rps_txn)
  tx_hash = web3.eth.send_raw_transaction(signed.rawTransaction)
  txn_logger.critical(f"Refund wager transaction hash: {web3.to_hex(tx_hash)}, game_id: {game.id}, payee: {payee.address}")
  tx_receipt = None

  # Get the transaction receipt for the decide winner transaction
  try:
    tx_receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    gas_used = tx_receipt.gasUsed

    # Calculate the total transaction cost in wei
    total_cost_wei = gas_used * web3.to_wei(fast_gas_price, 'gwei')

    # Convert wei to ether, if needed
    total_cost_eth = web3.from_wei(total_cost_wei, 'ether')
    logger.info(f'Actual total cost in ether: {total_cost_eth}')

    total_cost_usd = eth_to_usd(Decimal(str(total_cost_eth)))
    logger.info(f'Actual total cost in USD to refund wager: {total_cost_usd}')
    txn_logger.critical(f"Actual total cost in USD to refund wager: {total_cost_usd} for transaction hash: {web3.to_hex(tx_hash)}")
    logger.info(f'Transaction receipt after refundWager was called: {tx_receipt}')
    game.transactions.append(web3.to_hex(tx_hash))

    return tx_hash
  except web3.exceptions.TimeExhausted:
    txn_logger.critical(f"Timed out waiting for refund_wager transaction receipt: {web3.to_hex(tx_hash)}")
    return tx_hash

# def get_new_game():
#   return {
#     'id': str(uuid.uuid4()),
#     'player1': None,
#     'player2': None,
#     'winner': None,
#     'loser': None,
#     'contract_address': None,
#     'transactions': [],
#     'insufficient_funds': False,
#     'rpc_error': False,
#     'contract_rejected': False,
#     'game_over': False,
#     'uncaught_exception_occured': False
#   }

def get_new_player(player_id=None):
  return {
    'player_id': player_id,
    'game_id': None,
    'address': None,
    'choice': None,
    'wager': None,
    'winnings': 0,
    'losses': 0,
    'wager_accepted': False,
    'wager_offered': False,
    'contract_accepted': False,
    'contract_rejected': False,
    'rpc_error': False,
    'wager_refunded': False,
    'player_disconnected': False,
    'nonce': None
  }

@app.route('/get-wager', methods=['GET'])
def handle_get_wager():
  game_id = request.args.get('game_id')
  player_id = request.args.get('player_id')
  
  
  game = get_game(game_id)

  if not game:
    return

  logger.info(f"Current game state in on get_wager: {game}")

  if game.player1_id == player_id:    
    return { 'wager': game.player1_wager, 'contract_address': RPS_CONTRACT_ADDRESS }
  else:
    return { 'wager': game.player2_wager, 'contract_address': RPS_CONTRACT_ADDRESS }

@app.route('/ethereum-price', methods=['GET'])
def handle_get_ethereum_price():
  game_id = request.args.get('game_id')
  
  
  game = get_game(game_id)

  if not game:
    return

  return str(ethereum_prices[-1])

@app.route('/gas-oracle', methods=['GET'])
def handle_get_gas_oracle():
  game_id = request.args.get('game_id')
  
  
  game = get_game(game_id)

  if not game:
    return

  return gas_oracles[-1]

@app.route('/rps-contract-abi', methods=['GET'])
def get_rps_contract_abi():
  with open('contracts/RPSContractV2.json') as f:
    return json.load(f)

@socketio.on('rpc_error')
def handle_rpc_error(data):
  logger.error('RPC error: %s', data)

  game = get_game(data['game_id'])

  if game.game_over:    
    return
  
  payee_id = None  
  # determine which player got the RPC error
  if game.player1_id == data['player_id']:
    game.player1_rpc_error = True
    # did the other player accept the contract?
    if game.player2_contract_accepted:
      payee_id=game.player2_id
  else:
    game.player2_rpc_error = True
    # did the other player accept the contract?
    if game.player1_contract_accepted:
      payee_id=game.player1_id
  
  # game = update_game(game)

  # is there a payee?
  payee = game.get_player_data(payee_id)
  if payee is not None and not payee.wager_refunded:
    # refund the player that accepted the contract
    tx_hash = refund_wager(game, payee)
    
    if game.player1_id == payee_id:
      game.player1_wager_refunded = True
    else:
      game.player2_wager_refunded = True
    
    game.transactions.append(web3.to_hex(tx_hash))

    update_game(game)

    # send player a txn link to etherscan
    etherscan_link = None
    if 'sepolia' in args.env or 'ganache' in args.env:
      etherscan_link = f"https://sepolia.etherscan.io/tx/{web3.to_hex(tx_hash)}"
    elif 'mainnet' in args.env:
      etherscan_link = f"https://etherscan.io/tx/{web3.to_hex(tx_hash)}"
    emit('player_stake_refunded', { 'etherscan_link': etherscan_link }, room=payee['player_id'])
    
  game['rpc_error'] = True
  game.game_over = True

  update_game(game)

@socketio.on('insufficient_funds')  
def handle_insufficient_funds(data):
  logger.info('Player: %s had insufficient funds to join the contract.', data['player_id'])
  
  
  game = get_game(data['game_id'])

  if game.game_over:    
    return
    
  game['insufficient_funds'] = True
  game.game_over = True

  update_game(game)

def determine_refund_payee(game: Game, player_id: str) -> Optional[Player]:
  if game.player1_id == player_id:
    game.player1_contract_rejected = True
    # did the other player accept the contract?
    if game.player2_contract_accepted:
      return game.get_player_data(game.player2_id)
  else:
    game.player2_contract_rejected = True
    # did the other player accept the contract?
    if game.player1_contract_accepted:
      return game.get_player_data(game.player1_id)

  return None

@socketio.on('contract_rejected')
def handle_contract_rejected(data):
  
  game = get_game(data['game_id'])

  if game.game_over:
    return

  logging.info(f"Player {data['player_id']} rejected the contract.")

  game.game_over = True
  game.contract_rejected = True
  
  update_game(game)

  player_id = data['player_id']

  payee_id = None

  if game.player1_id == player_id:
    game.player1_contract_rejected = True
    # did the other player accept the contract?
    if game.player2_contract_accepted:
      payee_id = game.player2_id
  else:
    game.player2_contract_rejected = True
    # did the other player accept the contract?
    if game.player1_contract_accepted:
      payee_id = game.player1_id

  payee = None

  # is there a payee?
  payee = game.get_player_data(payee_id)
  if payee is not None and not payee.wager_refunded:
    # refund the player that accepted the contract
    tx_hash = refund_wager(game, payee=payee)
    
    if game.player1_id == data['player_id']:
      game.player1_wager_refunded = True
    else:
      game.player2_wager_refunded = True
      
    update_game(game)

    # send player a txn link to etherscan
    etherscan_link = None
    if 'sepolia' in args.env or 'ganache' in args.env:
      etherscan_link = f"https://sepolia.etherscan.io/tx/{web3.to_hex(tx_hash)}"
    elif 'mainnet' in args.env:
      etherscan_link = f"https://etherscan.io/tx/{web3.to_hex(tx_hash)}"
    emit('player_stake_refunded', { 'etherscan_link': etherscan_link }, room=payee['player_id'])
  
@socketio.on('pay_stake_confirmation')
def handle_pay_stake_confirmation(data):
  
  game = get_game(data['game_id'])
  logger.info('pay_stake_confirmation received: %s', data)
  logger.info(f"Current game state in on pay_stake_confirmation: {game}")

@socketio.on('paying_stake')
def handle_paying_stake(data):
  
  game = get_game(data['game_id'])
  logger.info(f"Notifying player their opponent has made a choice: {game}")

  if game.player1_id == data['player_id']: # player 1 accepted the contract
    # notify the other player that player1 accepted the contract
    emit('opponent_accepted_contract', room=game.player2_id)
  else:
    emit('opponent_accepted_contract', room=game.player1_id)

@socketio.on('pay_stake_hash')
def handle_join_contract_hash(data):
  logger.info('join contract transaction hash received: %s', data)
  game = get_game(data['game_id'])
  game.transactions.append(data['transaction_hash'])
  
  update_game(game)
  
  txn_logger.critical(f"Join contract transaction hash: {data['transaction_hash']}, game_id: {game.id}, address: {data['player_id']}")
  
  logger.info(f"Current game state in on join_contract_hash: {game}")

@socketio.on('pay_stake_receipt')
def handle_pay_stake_receipt(data):
  game = get_game(data['game_id'])

  # which player accepted the contract
  if game.player1_id == data['player_id']: # player 1 accepted the contract
    game.player1_address = data['address']
    game.player1_contract_accepted = True
  else:
    game.player2_address = data['address']
    game.player2_contract_accepted = True

  game = update_game(game)

  logger.info(f"Current game state in on pay_stake_receipt: {game}")

  if game.player1['contract_accepted'] and game.player2_contract_accepted:
    emit('both_players_accepted_contract', { 'contract_address': RPS_CONTRACT_ADDRESS }, room=game.player2_id)
    emit('both_players_accepted_contract', { 'contract_address': RPS_CONTRACT_ADDRESS }, room=game.player1_id)
    emit('nonce_requested', game.id, room=game.player1_id)
    emit('nonce_requested', game.id, room=game.player2_id)
    # settle_game(game.id)
    return
  
  # if the game is over, one player has already rejected the contract, or there were insufficient funds
  if game.game_over:
    payee = None
  
    # refund the player that just accepted the contract
    if game.player1_id == data['player_id']:
      payee = game.get_player_data(game.player1_id)
    else:
      payee = game.get_player_data(game.playe2_id)

    tx_hash = refund_wager(game, payee=payee)
    game.transactions.append(web3.to_hex(tx_hash))

    # send player a txn link to etherscan
    etherscan_link = None
    if 'sepolia' in args.env or 'ganache' in args.env:
      etherscan_link = f"https://sepolia.etherscan.io/tx/{web3.to_hex(tx_hash)}"
    elif 'mainnet' in args.env:
      etherscan_link = f"https://etherscan.io/tx/{web3.to_hex(tx_hash)}"
    
    if game['insufficient_funds']:
      logger.info('One player did not have the funds to join the contract. Notifying the opposing player and issuing a refund.')
      emit('player_stake_refunded', { 'etherscan_link': etherscan_link, 'reason': 'insufficient_funds' }, room=payee['player_id'])
    elif game['rpc_error']:
      logger.info('One player experienced an RPC error. Notifying the opposing player and issuing a refund.')
      emit('player_stake_refunded', { 'etherscan_link': etherscan_link, 'reason': 'rpc_error' }, room=payee['player_id'])
    else:
      logger.info('One player decided not to join the contract. Notifying the opposing player and issuing a refund.')
      emit('player_stake_refunded', { 'etherscan_link': etherscan_link, 'reason': 'contract_rejected' }, room=payee['player_id'])
  
    update_game(game)

@socketio.on('connect')
def handle_connect():
  params = request.args
  player_id = params.get('player_id')
  
  # create a new player
  new_player = Player(player_id=player_id)

  # Add the player to the in-memory queue
  player_queue.put(new_player)
  logger.info(f"Adding player with player_id {player_id} to queue")

  join_room(player_id)

  # Check if there are at least 2 players in the queue
  if player_queue.qsize() >= 2:
    # Remove the first two players from the queue
    player1 = player_queue.get()
    player2 = player_queue.get()

    # Try to join the game
    game = join_game(player1, player2)
    
    # If join_game returns None (e.g., due to same player_id), put player2 back in the queue
    if game is None:
      player_queue.put(player2)
      logger.info(f"Couldn't create game. Putting player {player2.player_id} back in the queue.")
    else:
      logger.info(f"Game created with id {game.id}")

  # Log the current number of players in the queue
  logger.info(f"Number of players in queue: {player_queue.qsize()}")

def join_game(player1: Player, player2: Player) -> Optional[Game]:
  if player1.player_id == player2.player_id:
    logger.info('Both players have the same ID. Cannot create game.')
    return None
  
  # Check if either player is already in an active game
  with get_db_connection() as conn:
    cursor = conn.cursor()
    for player in [player1, player2]:
      cursor.execute('''
        SELECT id FROM games 
        WHERE (player1_id = ? OR player2_id = ?) AND game_over = 0
      ''', (player.player_id, player.player_id))
      if cursor.fetchone():
        logger.info(f"Player {player.player_id} is already in an active game")
        return None

  # Create a new game
  game = Game(
    id=str(uuid.uuid4()),
    contract_address=RPS_CONTRACT_ADDRESS,
    player1_id=player1.player_id,
    player2_id=player2.player_id,
    player1_address=player1.address,
    player2_address=player2.address,
  )
  
  # Save the game to the database
  new_game = save_new_game_to_db(game)

  if new_game:
    # Emit an event to both players to inform them that the game has started
    emit('game_started', {'game_id': str(new_game.id)}, room=new_game.player1_id)
    emit('game_started', {'game_id': str(new_game.id)}, room=new_game.player2_id)
    logger.info(f"Game created with id {new_game.id}")
    return new_game
  else:
    logger.error("Failed to create game")
    return None
  
@socketio.on('offer_wager')
def handle_submit_wager(data):
  player_id = data['player_id']
  logger.info('Received wager from player_id %s', player_id)
  wager = data['wager']

  game = get_game(data['game_id'])
  # exit early if the game is over
  if game.game_over:
    logger.info('Game is over.')
    # log the address of the player that tried to submit a wager
    logger.info('Player %s tried to submit a wager.', player_id)
    return
  # assign the wager to the correct player  
  # if player1 offered the wager, emit an event to player2 to inform them that player1 offered a wager
  if player_id == game.player1_id and game.player2_wager_accepted == False:
    game.player1_wager = wager
    game.player1_wager_offered = True
    emit('wager_offered', {'wager': wager}, room=game.player2_id)
  else:
    if game.player1_wager_accepted == False:
      game.player2_wager = wager
      game.player2_wager_offered = True
      emit('wager_offered', {'wager': wager}, room=game.player1_id)
  
  update_game(game)

@socketio.on('accept_wager')
def handle_accept_wager(data):
  player_id = data['player_id']

  game = get_game(data['game_id'])
  # exit early if the game is over
  if game.game_over:
    logger.info('Game is over.')
    # log the address of the player that tried to submit a wager
    logger.info('Player %s tried to submit a wager.', player_id)
    return
  # mark the player as having accepted the wager
  if player_id == game.player1_id:
    game.player1_wager_accepted = True
    emit('wager_accepted', {'opp_wager_in_eth': data['opp_wager_in_eth']}, room=game.player2_id)
  else:
    game.player2['wager_accepted'] = True
    emit('wager_accepted', {'opp_wager_in_eth': data['opp_wager_in_eth']}, room=game.player1_id)

  logger.info('Player %s accepted the wager. Waiting for opponent.', player_id)

  if game.player1_wager_accepted and game.player2['wager_accepted']:
    logger.info(f"Current game state in on accept_wager after both players have accepted wagers: {game}")
    # notify both players that the contract has been created
    emit('both_wagers_accepted', {
      'contract_address': RPS_CONTRACT_ADDRESS, 
      'your_wager': game.player1_wager,
      'opponent_wager': game.player2_wager,
    }, room=game.player1_id)
    emit('both_wagers_accepted', {
      'contract_address': RPS_CONTRACT_ADDRESS,
      'your_wager': game.player2_wager,
      'opponent_wager': game.player1_wager,
    }, room=game.player2_id)

  update_game(game)

@socketio.on('decline_wager')
def handle_decline_wager(data):
  player_id = data['player_id']

  game = get_game(data['game_id'])
  # exit early if the game is over
  if game.game_over:
    logger.info('Game is over.')
    # log the address of the player that tried to submit a wager
    logger.info('Player %s tried to submit a wager.', player_id)
    return
  # mark the player as having declined the wager
  if game.player1_id == player_id:
    game.player1_wager_accepted = False
    # emit wager declined event to the opposing player
    emit('wager_declined', {'game_id': data['game_id']}, room=game.player2_id)
  else:
    game.player2['wager_accepted'] = False
    # emit wager declined event to the opposing player
    emit('wager_declined', {'game_id': data['game_id']}, room=game.player1_id)
  
  updated_game = update_game(game)
  # logger.info(f"Current game state in on decline_wager: {updated_game.model_dump_json()}")

@socketio.on('choice')
def handle_choice(data):
  player_id = data['player_id']

  game = get_game(data['game_id'])
  # exit early if the game is over
  if game.game_over:
    logger.info('Game is over.')
    # log the address of the player that tried to submit a wager
    logger.info('Player %s tried to submit a wager.', player_id)
    return
  # assign the choice to the player
  if game.player1_id == player_id:
    game.player1_choice = data['choice']
  else:
    game.player2_choice = data['choice']

  update_game(game)

  logger.info('Player {} chose: {}'.format(data['player_id'], data['choice']))

  if game.player1_choice and game.player2_choice:
    emit('both_players_chose', {'wager': game.player1_wager, 'contract_address': RPS_CONTRACT_ADDRESS }, room=game.player1_id)
    emit('both_players_chose', {'wager': game.player2_wager, 'contract_address': RPS_CONTRACT_ADDRESS }, room=game.player2_id)
            
def settle_game(game_id=None):
  game = get_game(game_id)

  game = determine_winner(game)
  winner_address = None

  player_1_stake_in_ether = usd_to_eth(float(game.player1_wager.replace('$', '')))
  logger.info(f"Player 1 stake in ether: {player_1_stake_in_ether}")
  player_1_stake_in_wei = web3.to_wei(player_1_stake_in_ether, 'ether')
  logger.info(f"Player 1 stake in wei: {player_1_stake_in_wei}")

  player_2_stake_in_ether = usd_to_eth(float(game.player2_wager.replace('$', '')))
  logger.info(f"Player 2 stake in ether: {player_2_stake_in_ether}")
  player_2_stake_in_wei = web3.to_wei(player_2_stake_in_ether, 'ether')
  logger.info(f"Player 2 stake in wei: {player_2_stake_in_wei}")
      
  logger.info('Calling payWinner contract function')
  
  rps_contract_address = web3.to_checksum_address(RPS_CONTRACT_ADDRESS)
  logger.info(f"Contract address: {rps_contract_address}")

  global rps_contract_abi
  rps_contract = web3.eth.contract(address=rps_contract_address, abi=rps_contract_abi)
  rps_txn = None
  
  gas_oracle = gas_oracles[-1] # get_gas_oracle()

  fast_gas_price = float(gas_oracle['result']['FastGasPrice'])
  logger.info(f"Fast gas price for payWinner: {fast_gas_price}")

  suggest_base_fee = float(gas_oracle['result']['suggestBaseFee'])
  logger.info(f"Suggested base fee for payWinner: {suggest_base_fee}")

  max_priority_gas_fee = round(fast_gas_price) - round(suggest_base_fee)
  logger.info(f"Max priority gas fee for payWinner: {max_priority_gas_fee}")

  contract_owner_account = Account.from_key(CONTRACT_OWNER_PRIVATE_KEY)
  logger.info(f"Contract owner account address: {contract_owner_account.address}")
  contract_owner_checksum_address = web3.to_checksum_address(contract_owner_account.address)

  # Sign transaction using the private key of the contract owner account
  nonce = web3.eth.get_transaction_count(contract_owner_checksum_address)  # Get the nonce

  logger.info(f"Game state before calling payWinner or payDraw: {game}")

  if game.winner is None and game.loser is None:
    logger.info('Game is a draw.')
    
    player_1_address = game.player1_address
    player_2_address = game.player2_address

    p1_draw_fee = player_1_stake_in_ether * 0.03
    p2_draw_fee = player_2_stake_in_ether * 0.03
    draw_game_fee = p1_draw_fee + p2_draw_fee
    logger.info(f"Draw game fee: {draw_game_fee}")

    gas_estimate_txn = rps_contract.functions.payDraw(web3.to_checksum_address(player_1_address),
                                                      web3.to_checksum_address(player_2_address),
                                                      player_1_stake_in_wei,
                                                      player_2_stake_in_wei,
                                                      web3.to_wei(draw_game_fee, 'ether'), 
                                                      game.id).build_transaction({
      'from': contract_owner_checksum_address
    })

    gas_estimate = web3.eth.estimate_gas(gas_estimate_txn)
    logger.info(f"Gas estimate for payDraw txn: {gas_estimate}")

    # Calculate the estimated transaction cost in wei
    est_total_cost_wei = gas_estimate * web3.to_wei(gas_oracle['result']['SafeGasPrice'], 'gwei')

    # Convert wei to ether, if needed
    est_total_cost_eth = web3.from_wei(est_total_cost_wei, 'ether')
    logger.info(f'Estimated total cost in ether to settle draw: {est_total_cost_eth}')

    gas_fee_premium = Decimal(str(1.12))
    est_total_cost_usd = eth_to_usd(Decimal(str(est_total_cost_eth)))
    logger.info(f'Estimated total cost in USD to settle draw: {est_total_cost_usd}')
    logger.info(f"Estimated total cost with a premium of 12%: {est_total_cost_usd * gas_fee_premium}")
    txn_logger.critical(f"Estimated total cost in USD to settle draw: {est_total_cost_usd}")

    if est_total_cost_eth * gas_fee_premium > draw_game_fee:
      draw_game_fee = float(est_total_cost_eth * gas_fee_premium)

    rps_txn = rps_contract.functions.payDraw(web3.to_checksum_address(player_1_address), 
                                          web3.to_checksum_address(player_2_address), 
                                          player_1_stake_in_wei, 
                                          player_2_stake_in_wei,
                                          web3.to_wei(draw_game_fee, 'ether'),
                                          game.id).build_transaction({
      'from': contract_owner_checksum_address,
      'nonce': nonce,
      'gas': gas_estimate,
      'maxFeePerGas': web3.to_wei(gas_oracle['result']['SafeGasPrice'], 'gwei'),
      'maxPriorityFeePerGas': web3.to_wei(str(max_priority_gas_fee), 'gwei')
    })
  else:
    winning_player = game.get_player_data(game.winner)
    losing_player = game.get_player_data(game.loser)
    winner_address = winning_player.address
    logger.info(f"Winner account address: {winner_address}")

    p1_win_game_fee = player_1_stake_in_ether * 0.1
    p2_win_game_fee = player_2_stake_in_ether * 0.1
    win_game_fee = p1_win_game_fee + p2_win_game_fee
    logger.info(f"Win game fee: {win_game_fee}")

    logger.info('Winning player: {}'.format(winning_player.model_dump_json()))
    logger.info('Losing player: {}'.format(losing_player.model_dump_json()))

    gas_estimate_txn = rps_contract.functions.payWinner(web3.to_checksum_address(winner_address), 
                                                    player_1_stake_in_wei,
                                                    player_2_stake_in_wei,
                                                    web3.to_wei(win_game_fee, 'ether'), 
                                                    game.id).build_transaction({
      'from': contract_owner_checksum_address
    })

    gas_estimate = web3.eth.estimate_gas(gas_estimate_txn)
    logger.info(f"Gas estimate for payWinner txn: {gas_estimate}")

    # Calculate the estimated transaction cost in wei
    est_total_cost_wei = gas_estimate * web3.to_wei(gas_oracle['result']['SafeGasPrice'], 'gwei')

    # Convert wei to ether, if needed
    est_total_cost_eth = web3.from_wei(est_total_cost_wei, 'ether')
    logger.info(f'Estimated total cost in ether to pay winner: {est_total_cost_eth}')

    gas_fee_premium = Decimal(str(1.12))
    est_total_cost_usd = eth_to_usd(Decimal(str(est_total_cost_eth)))
    logger.info(f'Estimated total cost in USD to pay winner: {est_total_cost_usd}')
    logger.info(f"Estimated total cost with a premium of 12%: {est_total_cost_usd * gas_fee_premium}")
    txn_logger.critical(f"Estimated total cost in USD to pay winner: {est_total_cost_usd}")

    if est_total_cost_eth * gas_fee_premium > win_game_fee:
      win_game_fee = float(est_total_cost_eth * gas_fee_premium)

    loser_wager_in_eth = usd_to_eth(float(winning_player.wager.replace('$', '')))
    winnings = loser_wager_in_eth - win_game_fee
    logger.info(f"Winnings minus fee: {winnings}")

    winnings_in_usd = eth_to_usd(Decimal(str(winnings)))
    winnings_to_float = '{:.2f}'.format(float(winnings_in_usd))

    # assign winnings to the winning player
    if game.winner == game.player1_id:
      game.player1_winnings = winnings_to_float
      game.player2_losses = losing_player.wager
    else :
      game.player2_winnings = winnings_to_float
      game.player1_losses = losing_player.wager
    
    rps_txn = rps_contract.functions.payWinner(
        web3.to_checksum_address(winner_address), 
        player_1_stake_in_wei, 
        player_2_stake_in_wei, 
        web3.to_wei(win_game_fee, 'ether'), 
        game.id).build_transaction({
      'from': contract_owner_checksum_address,
      'gas': gas_estimate,
      'nonce': nonce,
      'maxFeePerGas': web3.to_wei(gas_oracle['result']['SafeGasPrice'], 'gwei'),
      'maxPriorityFeePerGas': web3.to_wei(str(max_priority_gas_fee), 'gwei')
    })

  update_game(game)

  tx_hash = None
  tx_receipt = None

  if 'sepolia' in args.env:
    # running on the Sepolia testnet
    logger.info("Running on Sepolia testnet")
  elif 'mainnet' in args.env:
    # running on the Ethereum mainnet
    logger.info("Running on Ethereum mainnet")
  
  try:
    signed = contract_owner_account.sign_transaction(rps_txn)
    tx_hash = web3.eth.send_raw_transaction(signed.rawTransaction)
    
    if game.winner is None and game.loser is None:
      txn_logger.critical(f"Game resulted in a draw transaction hash: {web3.to_hex(tx_hash)}, game_id: {game.id}, address: {contract_owner_account.address}, player1: {game.player1_id}, player2: {game.player2_id}")
    else:
      txn_logger.critical(f"Pay winner transaction hash: {web3.to_hex(tx_hash)}, game_id: {game.id}, address: {contract_owner_account.address}, winner: {winner['address']}, loser: {loser['address']}")
    
    tx_receipt = None

    # Get the transaction receipt for the decide winner transaction
    tx_receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    gas_used = tx_receipt.gasUsed

    # Calculate the total transaction cost in wei
    total_cost_wei = gas_used * web3.to_wei(gas_oracle['result']['SafeGasPrice'], 'gwei')

    # Convert wei to ether, if needed
    total_cost_eth = web3.from_wei(total_cost_wei, 'ether')
    logger.info(f'Actual cost in ether to settle game: {total_cost_eth}')

    logger.info(f'Transaction receipt after payWinner was called: {tx_receipt}')

    total_cost_usd = eth_to_usd(Decimal(str(total_cost_eth)))
    logger.info(f'Actual cost in USD to settle game: {total_cost_usd}')
    txn_logger.critical(f"Actual cost in USD to settle game: {total_cost_usd} for transaction hash: {web3.to_hex(tx_hash)}")
    game.transactions.append(web3.to_hex(tx_hash))
  except ValueError as e:
    if e['message'] != 'already known':
      # notify both players there was an error while settling the bet
      emit('pay_winner_error', room=game.player1_id)
      emit('pay_winner_error', room=game.player2_id)
    logger.error(f"A ValueError occurred: {str(e)}")
    logger.error(f"Error message {e['message']}")
  except Exception as e:
    # notify both players there was an error while settling the bet
    emit('pay_winner_error', room=game.player1_id)
    emit('pay_winner_error', room=game.player2_id)
    logger.error(f"An error occurred: {str(e)}")
    logger.error(f"Message {e['message']}")
  except web3.exceptions.TimeExhausted:
    txn_logger.critical(f"Timed out waiting for transaction receipt for settle_game transaction: {web3.to_hex(tx_hash)}")
  
  # GAME OVER, man!
  game.game_over = True

  logger.info(f"Current game state in on choice after paying winner: {game.model_dump_json()}")
  logger.info(f"Final game state: {game.model_dump_json()}")

  update_game(game)

  etherscan_link = None
  # send player a txn link to etherscan
  if 'sepolia' in args.env or 'ganache' in args.env:
    etherscan_link = f"https://sepolia.etherscan.io/tx/{web3.to_hex(tx_hash)}"
  elif 'mainnet' in args.env:
    etherscan_link = f"https://etherscan.io/tx/{web3.to_hex(tx_hash)}"

  if game.winner is None and game.loser is None:
    # emit an event to both players to inform them that the game is a draw
    emit('draw', {
      'etherscan_link': etherscan_link,
      'your_choice': game.player1_choice,
      'opp_choice': game.player2_choice}, room=game.player1_id)
    emit('draw', {
      'etherscan_link': etherscan_link,
      'your_choice': game.player2_choice,
      'opp_choice': game.player1_choice}, room=game.player2_id)
  else:
    # emit an event to both players to inform them of the result
    emit('you_win', {
      'etherscan_link': etherscan_link,
      'your_choice': winning_player.choice,
      'opp_choice': losing_player.choice,
      'winnings': winning_player.winnings
      }, room=game.winner)
    emit('you_lose', {
      'etherscan_link': etherscan_link,
      'your_choice': losing_player.choice,
      'opp_choice': winning_player.choice, 
      'losses': losing_player.losses
      }, room=game.loser)
    
@socketio.on('disconnect')
def handle_disconnect():
  player_id = request.args.get('player_id')
  logger.info('Player with player_id {} disconnected.'.format(player_id))

  QUERY = "SELECT * FROM games g WHERE g.game_over = false"
  results = cosmos_db.query_items(query=QUERY, enable_cross_partition_query=True)
  games = [game for game in results]

  for game in games:
    game.game_over = True
    update_game(game)
    # do we need to issue a refund?
    if game.player1_id == player_id:
      logger.info('Player1 {} disconnected from game {}.'.format(player_id, game.id))
      game.player1_disconnected = True
      # do we need to issue a refund?
      if game.player2_contract_accepted and not game.player2_wager_refunded:
        logger.info('Player2 accepted the contract. Issuing a refund.')
        payee = game.get_player_data(game.player2_id)
        tx_hash = refund_wager(game, payee)
        game.transactions.append(web3.to_hex(tx_hash))
        update_game(game)
        # send player a txn link to etherscan
        etherscan_link = None
        if 'sepolia' in args.env or 'ganache' in args.env:
          etherscan_link = f"https://sepolia.etherscan.io/tx/{web3.to_hex(tx_hash)}"
        elif 'mainnet' in args.env:
          etherscan_link = f"https://etherscan.io/tx/{web3.to_hex(tx_hash)}"
        emit('player_stake_refunded', { 'etherscan_link': etherscan_link }, room=game.player2_id)
      emit('opponent_disconnected', room=game.player2_id)
    elif game.player2_id == player_id:
      logger.info('Player2 {} disconnected from game {}.'.format(player_id, game.id))
      game.player2['player_disconnected'] = True
      if game.player1['contract_accepted'] and not game.player1_wager_refunded:
        logger.info('Player1 accepted the contract. Issuing a refund.')
        payee = game.get_player_data(game.player1_id)
        tx_hash = refund_wager(game, payee)
        game.transactions.append(web3.to_hex(tx_hash))
        update_game(game)
        # send player a txn link to etherscan
        etherscan_link = None
        if 'sepolia' in args.env or 'ganache' in args.env:
          etherscan_link = f"https://sepolia.etherscan.io/tx/{web3.to_hex(tx_hash)}"
        elif 'mainnet' in args.env:
          etherscan_link = f"https://etherscan.io/tx/{web3.to_hex(tx_hash)}"
        emit('player_stake_refunded', { 'etherscan_link': etherscan_link }, room=game.player1_id)
      emit('opponent_disconnected', room=game.player1_id)
    
    update_game(game)

def global_exception_handler(type, value, traceback):
  txn_logger.error('!!!UNCAUGHT EXCEPTION OCCURED!!!')
  txn_logger.error(f"UNCAUGHT EXCEPTION TYPE: {type}")
  txn_logger.error(f"UNCAUGHT EXCEPTION VALUE: {value}")
  
  # determine if refunds need to be issued
  QUERY = "SELECT * FROM games g WHERE g.game_over = false"
  results = cosmos_db.query_items(query=QUERY, enable_cross_partition_query=True)
  games = [game for game in results]

  for game in games:
    game.game_over = True
    if game.player2_contract_accepted and not game.player2_wager_refunded:
      logger.info('Player2 accepted the contract. Issuing a refund.')
      payee = game.get_player_data(game.player2_id)
      tx_hash = refund_wager(game, payee)
      game.transactions.append(web3.to_hex(tx_hash))
      game.player2_wager_refunded = True
      # send player a txn link to etherscan
      etherscan_link = None
      if 'sepolia' in args.env or 'ganache' in args.env:
        etherscan_link = f"https://sepolia.etherscan.io/tx/{web3.to_hex(tx_hash)}"
      elif 'mainnet' in args.env:
        etherscan_link = f"https://etherscan.io/tx/{web3.to_hex(tx_hash)}"
      emit('uncaught_exception_occured', { 'etherscan_link': etherscan_link }, room=game.player2_id)

    if game.player1['contract_accepted'] and not game.player1_wager_refunded:
      logger.info('Player1 accepted the contract. Issuing a refund.')
      payee = game.get_player_data(game.player2_id)
      tx_hash = refund_wager(game, payee)
      game.transactions.append(web3.to_hex(tx_hash))
      game.player1_wager_refunded = True
      # send player a txn link to etherscan
      etherscan_link = None
      if 'sepolia' in args.env or 'ganache' in args.env:
        etherscan_link = f"https://sepolia.etherscan.io/tx/{web3.to_hex(tx_hash)}"
      elif 'mainnet' in args.env:
        etherscan_link = f"https://etherscan.io/tx/{web3.to_hex(tx_hash)}"
      emit('uncaught_exception_occured', { 'etherscan_link': etherscan_link }, room=game.player1_id)

sys.excepthook = global_exception_handler

def get_eth_prices():
  while True:
    current_price = get_eth_price()
    logger.info(f"Current price of Ethereum: {current_price}")
    global ethereum_prices
    ethereum_prices.append(current_price)

    if len(ethereum_prices) > 10:
      ethereum_prices = ethereum_prices[-5:]

    time.sleep(45)

def get_gas_oracles():
  while True:
    gas_oracle = get_gas_oracle()
    logger.info(f"Gas oracle: {gas_oracle}")
    global gas_oracles
    gas_oracles.append(gas_oracle)

    if len(gas_oracles) > 10:
      gas_oracles = gas_oracles[-5:]

    time.sleep(30)

if __name__ == '__main__':
  from geventwebsocket.handler import WebSocketHandler
  from gevent.pywsgi import WSGIServer
  
  logger.info('Downloading contract ABIs...')
  download_contract_abi()

  thread = threading.Thread(target=get_eth_prices)
  thread.start()
  
  thread = threading.Thread(target=get_gas_oracles)
  thread.start()

  logger.info('Starting server...')

  # http_server = WSGIServer(('0.0.0.0', 443),
  #                          app,
  #                          keyfile=KEYFILE,
  #                          certfile=CERTFILE,
  #                          handler_class=WebSocketHandler)

  http_server = WSGIServer(('0.0.0.0', 8080),
                           app,                        
                           handler_class=WebSocketHandler)

  http_server.serve_forever()
