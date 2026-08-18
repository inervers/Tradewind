export function formatLocation(country = "", city = ""): string {
  const cleanCountry = country.trim().replace(/\s+/g, " ");
  const cleanCity = city.trim().replace(/\s+/g, " ");
  if (!cleanCountry) return cleanCity;
  if (!cleanCity) return cleanCountry;
  if (cleanCountry.toLocaleLowerCase() === cleanCity.toLocaleLowerCase()) return cleanCountry;
  return `${cleanCountry} ${cleanCity}`;
}
