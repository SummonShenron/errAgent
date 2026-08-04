import React, { createContext, useContext, useEffect, useState } from 'react';
import { useUser, useAuth } from '@clerk/clerk-react';

interface UserContextType {
  principal: string | null;
  isReady: boolean;
  getToken: () => Promise<string | null>;
}

const UserContext = createContext<UserContextType>({
  principal: null,
  isReady: false,
  getToken: async () => null,
});

export const UserProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { user, isLoaded } = useUser();
  const { getToken } = useAuth();
  const [isReady, setIsReady] = useState(false);

  useEffect(() => {
    if (isLoaded) {
      if (user?.primaryEmailAddress?.emailAddress) {
        localStorage.setItem('principal', user.primaryEmailAddress.emailAddress);
      } else if (!user) {
        // Fallback for guest mode when no user is signed in
        localStorage.setItem('principal', 'guest-recruiter@example.com');
      }
      setIsReady(true);
    }
  }, [isLoaded, user]);

  return (
    <UserContext.Provider
      value={{
        principal: localStorage.getItem('principal'),
        isReady,
        getToken,
      }}
    >
      {isReady ? children : <div style={{ padding: '20px' }}>Loading session...</div>}
    </UserContext.Provider>
  );
};

export const useAppUser = () => useContext(UserContext);