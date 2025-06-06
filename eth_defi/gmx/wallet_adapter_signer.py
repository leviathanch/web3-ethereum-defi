from typing import Union, Any, Dict, Optional, cast

from eth_account.messages import encode_defunct
from eth_typing import ChecksumAddress
from cchecksum import to_checksum_address
from hexbytes import HexBytes

from web3 import Web3
from web3.types import SignedTx, TxParams
from eth_defi.basewallet import BaseWallet
from eth_defi.hotwallet import HotWallet, SignedTransactionWithNonce
from eth_defi.provider_wallet import Web3ProviderWallet

from gmx_python_sdk.scripts.v2.signers import Signer


class WalletAdapterSigner(Signer):
    """Adapter that makes any BaseWallet implementation work with GMX's Signer interface.

    This adapter handles the different behaviors of various wallet types:
    - HotWallet: Direct signing through its account object for transactions with nonces
    - Web3ProviderWallet: Delegating to the provider for sending transactions
    - Other BaseWallet implementations: Using their sign_transaction_with_new_nonce method
    """

    def __init__(self, wallet: Union[BaseWallet, HotWallet, Web3ProviderWallet], web3: Web3) -> None:
        """Initialize the adapter with a wallet and web3 instance.

        Args:
            wallet: Any implementation of BaseWallet
            web3: The Web3 instance connected to the blockchain
        """
        self.wallet = wallet
        self.web3 = web3

    def get_address(self) -> ChecksumAddress:
        """Get the wallet's address.

        Returns:
            The Ethereum address associated with this wallet
        """
        return to_checksum_address(self.wallet.get_main_address())

    def sign_message(self, message: Union[str, bytes, int]) -> HexBytes:
        """Sign a message with the wallet.

        This method adapts to different wallet types to sign a message.

        Args:
            message: The message to sign, which can be a string, bytes, or integer

        Returns:
            The signed message as a HexBytes object

        Raises:
            NotImplementedError: If the wallet type doesn't support direct message signing
            ValueError: If there's an error during the signing process
        """
        # Convert message to bytes if it's not already
        if isinstance(message, str):
            # Check if it's a hex string
            if message.startswith("0x"):
                signable_message = encode_defunct(HexBytes(message))
            else:
                signable_message = encode_defunct(message.encode("utf-8"))
        elif isinstance(message, int):
            signable_message = encode_defunct(Web3.to_bytes(message))
        elif isinstance(message, bytes) or isinstance(message, HexBytes):
            signable_message = encode_defunct(message)
        else:
            raise ValueError(f"Unsupported message type: {type(message)}")

        try:
            # HotWallet has an account object we can use directly
            if isinstance(self.wallet, HotWallet):
                # Use the account's sign method
                signature = self.wallet.account.sign_message(signable_message)
                return HexBytes(signature.signature)

            elif isinstance(self.wallet, Web3ProviderWallet):
                # For provider wallets, use personal_sign
                raise NotImplementedError("Message signing not implemented for wallet type: Web3ProviderWallet")

            else:
                # Try using the wallet's sign_message method if available
                if hasattr(self.wallet, "sign_message"):
                    return HexBytes(self.wallet.sign_message(signable_message).messageHash)

                raise NotImplementedError(f"Message signing not implemented for wallet type: {type(self.wallet)}")

        except Exception as e:
            raise ValueError(f"Failed to sign message: {str(e)}") from e

    def sign_transaction(self, unsigned_tx: TxParams) -> Union[SignedTransactionWithNonce, SignedTx]:
        """Sign a transaction with the wallet.

        This method handles different wallet types and transactions with or without nonces.

        Args:
            unsigned_tx: The transaction to sign

        Returns:
            A signed transaction object

        Raises:
            NotImplementedError: If direct signing with nonce is not supported for Web3ProviderWallet
            ValueError: If there's an error during the signing process
        """
        # Handle transactions that already have a nonce
        if "nonce" in unsigned_tx:
            # HotWallet has an account object we can use directly
            if isinstance(self.wallet, HotWallet):
                # Sign directly with the account to bypass HotWallet's nonce assertion
                result = self.wallet.account.sign_transaction(unsigned_tx)
                # Update the wallet's nonce tracking to stay in sync
                if self.wallet.current_nonce is not None:
                    self.wallet.current_nonce = max(unsigned_tx["nonce"] + 1, self.wallet.current_nonce)
                return result
            elif isinstance(self.wallet, Web3ProviderWallet):
                # Web3ProviderWallet needs to delegate to the provider
                # But we can't directly sign, only send
                raise NotImplementedError("Direct signing with nonce not supported for Web3ProviderWallet")
            else:
                # For other wallet types, we need to extract nonce, sign, and update
                nonce = unsigned_tx.pop("nonce")
                result = self.wallet.sign_transaction_with_new_nonce(unsigned_tx)
                # Restore the nonce value in the original transaction
                unsigned_tx["nonce"] = nonce
                # Update the wallet's nonce tracking
                if hasattr(self.wallet, "current_nonce") and self.wallet.current_nonce is not None:
                    self.wallet.current_nonce = max(nonce + 1, self.wallet.current_nonce)
                return result
        else:
            # No nonce provided, use the wallet's nonce management
            return self.wallet.sign_transaction_with_new_nonce(unsigned_tx)

    def send_transaction(self, unsigned_tx: TxParams) -> HexBytes:
        """Sign and send a transaction.

        This method handles different wallet types and adapts to their capabilities.

        Args:
            unsigned_tx: The transaction to sign and send

        Returns:
            The transaction hash as a HexBytes object

        Raises:
            ValueError: If there's an error during the transaction sending process
        """
        # Handle Web3ProviderWallet specially
        if isinstance(self.wallet, Web3ProviderWallet):
            # Remove the "from" field - the provider will add it
            tx_copy = dict(unsigned_tx)
            if "from" in tx_copy:
                del tx_copy["from"]

            # Use the wallet's nonce management if no nonce is provided
            if "nonce" not in tx_copy and hasattr(self.wallet, "allocate_nonce"):
                tx_copy["nonce"] = self.wallet.allocate_nonce()

            # Send through the provider
            return self.web3.eth.send_transaction(tx_copy)

        # For other wallet types, sign and then send the raw transaction
        try:
            signed_tx = self.sign_transaction(unsigned_tx)

            # Extract the raw transaction bytes
            if hasattr(signed_tx, "rawTransaction"):
                raw_tx = cast(Any, signed_tx).rawTransaction
            elif hasattr(signed_tx, "raw_transaction"):
                raw_tx = cast(Any, signed_tx).raw_transaction
            elif isinstance(signed_tx, SignedTransactionWithNonce):
                raw_tx = signed_tx.rawTransaction
            else:
                raise ValueError(f"Unknown signed transaction format: {type(signed_tx)}")

            # Send the raw transaction
            return self.web3.eth.send_raw_transaction(raw_tx)
        except Exception as e:
            raise ValueError(f"Failed to send transaction: {e!s}") from e
