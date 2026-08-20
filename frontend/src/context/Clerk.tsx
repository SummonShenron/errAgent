import React, { createContext, useContext, useEffect, useState } from 'react';
import { useUser, useAuth } from '@clerk/clerk-react';

interface UserContextType {
  principal: string | null;
  isReady: boolean;
  isSignedIn: boolean;
  getToken: () => Promise<string | null>;
}

const UserContext = createContext<UserContextType>({
  principal: null,
  isReady: false,
  isSignedIn: false,
  getToken: async () => null,
});

export const UserProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { user, isLoaded } = useUser();
  const { getToken: getClerkToken } = useAuth();
  const [isReady, setIsReady] = useState(false);

  useEffect(() => {
    if (isLoaded) {
      if (user?.primaryEmailAddress?.emailAddress) {
        localStorage.setItem('principal', user.primaryEmailAddress.emailAddress);
      } else if (!user) {
        localStorage.removeItem('principal');
      }
      setIsReady(true);
    }
  }, [isLoaded, user]);

  return (
    <UserContext.Provider
      value={{
        principal: localStorage.getItem('principal'),
        isReady,
        isSignedIn: Boolean(user),
        getToken: () => getClerkToken(),
      }}
    >
      {isReady ? children : <div style={{ padding: '20px' }}>Loading session...</div>}
    </UserContext.Provider>
  );
};

export const useAppUser = () => useContext(UserContext);